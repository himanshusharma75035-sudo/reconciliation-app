"""
routes/bbps.py

BBPS (bill payment / recharge) reconciliation API.

Flow:
  1. Upload internal Simplibank dump (SMB_..._UR)   → POST /api/bbps/upload-internal
  2. Upload operator statement (Moneyart / Levin)    → POST /api/bbps/upload-bank   (provider auto-detected)
  3. Run reconciliation                              → POST /api/bbps/run-recon
  4. View summary / results / export / fix manually  → GET/POST /api/bbps/...
"""
import io
import json
import datetime
import logging

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from models.database import get_db, generate_id, AuditLog, BbpsInternal, BbpsBankTxn
from core.auth import get_current_user, require_product_access
from core import bbps_engine as BB
from core import maker_checker

logger = logging.getLogger("eko_recon")
router = APIRouter(prefix="/api/bbps", tags=["bbps"],
                   dependencies=[Depends(require_product_access("bbps"))])
_TODAY = lambda: datetime.date.today().isoformat()

STATUS_LABELS = {
    "matched": "Matched", "failed_refunded": "Failed & Refunded",
    "failed_pending_refund": "Failed — Pending Refund", "refunded_but_success": "Refunded but Operator Success",
    "amount_mismatch": "Amount Mismatch", "unmatched_internal": "Unmatched (Internal)",
    "unmatched_bank": "Unmatched (Operator)",
}
OVERRIDE_STATUSES = ["matched", "failed_refunded", "failed_pending_refund", "refunded_but_success",
                     "written_off", "under_review", "unmatched_internal", "unmatched_bank"]


def _audit(db, user, action, detail, entity_id=None, prev=None):
    db.add(AuditLog(id=generate_id(), user_id=getattr(user, "id", None),
                    username=getattr(user, "username", None), action=action, action_type="human",
                    entity_type="bbps", entity_id=entity_id,
                    detail=json.dumps(detail, default=str),
                    previous_state=json.dumps(prev, default=str) if prev else None))


# ─── Uploads ──────────────────────────────────────────────────────────────────
_BBPS_PAIRED = ("matched", "amount_mismatch", "failed_refunded",
                "failed_pending_refund", "refunded_but_success")


@router.post("/upload-internal")
async def upload_internal(file: UploadFile = File(...), force: bool = Form(False),
                          db: Session = Depends(get_db),
                          user=Depends(get_current_user)):
    raw = await file.read()
    from core.file_hash_guard import guard_duplicate_file
    # force= restores rows removed since the original upload (e.g. by a Clear) — the
    # upsert-per-eko_trxn_id makes a forced re-apply idempotent (parser drops eko-less
    # rows, so nothing can double).
    guard_duplicate_file(db, "bbps", "internal", raw, file.filename, user, force=force)
    try:
        recs = BB.parse_internal(raw, file.filename)
    except Exception as e:
        raise HTTPException(400, f"Could not parse internal dump: {e}")
    if not recs:
        raise HTTPException(400, "No BBPS (Payments & Recharge) rows found in the dump.")
    upload_date = _TODAY()
    inserted = replaced = 0
    for r in recs:
        ex = db.query(BbpsInternal).filter(BbpsInternal.eko_trxn_id == r["eko_trxn_id"]).first()
        if ex:
            if ex.match_id:
                # Replacing a matched internal row orphans its bank leg — reset it (the
                # next run-recon would also heal it, but never leave a stale link).
                db.query(BbpsBankTxn).filter(
                    BbpsBankTxn.match_id == ex.match_id,
                    BbpsBankTxn.recon_status.in_(_BBPS_PAIRED),
                ).update({"recon_status": "unmatched_bank", "match_id": None,
                          "match_note": "internal leg replaced by re-upload — auto-reverted"},
                         synchronize_session=False)
            db.delete(ex); replaced += 1
        db.add(BbpsInternal(
            id=generate_id(), upload_date=upload_date, eko_trxn_id=r["eko_trxn_id"],
            provider=r["provider"], source=r.get("source"), amount=r["amount"], status=r["status"],
            is_refunded=r["is_refunded"], transaction_date=r["transaction_date"],
            csp_code=r.get("csp_code"), merchant_name=r.get("merchant_name"),
            cell_number=r.get("cell_number"), tracking_number=r.get("tracking_number")))
        inserted += 1
    _audit(db, user, "bbps_upload_internal", {"file": file.filename, "rows": inserted, "replaced": replaced})
    db.commit()
    from collections import Counter
    return {"message": "Internal dump ingested", "rows": inserted, "replaced": replaced,
            "by_provider": dict(Counter(r["provider"] for r in recs))}


@router.post("/upload-bank")
async def upload_bank(file: UploadFile = File(...), force: bool = Form(False),
                      db: Session = Depends(get_db),
                      user=Depends(get_current_user)):
    raw = await file.read()
    from core.file_hash_guard import guard_duplicate_file
    # force= restores rows removed since the original upload — safe: this path is a full
    # replace per provider, so a forced re-apply supersedes rather than duplicates.
    guard_duplicate_file(db, "bbps", "bank", raw, file.filename, user, force=force)
    try:
        provider, rows = BB.parse_bank_statement(raw, file.filename)
    except Exception as e:
        raise HTTPException(400, str(e))
    if not rows:
        raise HTTPException(400, "No transactions found in the statement.")
    # Replace prior rows for this provider (fresh statement supersedes). The wipe removes
    # matched bank legs too — reset their internal counterparts so no internal row stays
    # matched pointing at a deleted bank row (run-recon right after also heals, but never
    # leave the window).
    _wiped_mids = {m for (m,) in db.query(BbpsBankTxn.match_id)
                   .filter(BbpsBankTxn.provider == provider,
                           BbpsBankTxn.match_id.isnot(None)).all()}
    if _wiped_mids:
        db.query(BbpsInternal).filter(
            BbpsInternal.match_id.in_(_wiped_mids),
            BbpsInternal.recon_status.in_(_BBPS_PAIRED),
        ).update({"recon_status": "unmatched_internal", "match_id": None,
                  "match_note": "bank statement replaced — auto-reverted"},
                 synchronize_session=False)
    db.query(BbpsBankTxn).filter(BbpsBankTxn.provider == provider).delete()
    upload_date = _TODAY()
    for r in rows:
        db.add(BbpsBankTxn(
            id=generate_id(), upload_date=upload_date, provider=provider, client_ref=r["client_ref"],
            operator_ref=r.get("operator_ref"), order_id=r.get("order_id"), amount=r["amount"],
            status=r["status"], transaction_date=r["transaction_date"], service_name=r.get("service_name"),
            operator_name=r.get("operator_name"), reason=r.get("reason"),
            recharge_product=r.get("recharge_product")))
    _audit(db, user, "bbps_upload_bank", {"provider": provider, "file": file.filename, "rows": len(rows)})
    db.commit()
    cr = sum(1 for r in rows if (r["status"] or "").lower().startswith("succ"))
    return {"message": "Operator statement ingested", "provider": provider, "rows": len(rows),
            "success": cr, "failed": len(rows) - cr}


# ─── Run reconciliation ───────────────────────────────────────────────────────
@router.post("/run-recon")
def run_recon(db: Session = Depends(get_db), user=Depends(get_current_user)):
    int_objs = db.query(BbpsInternal).all()
    bank_objs = db.query(BbpsBankTxn).all()
    if not int_objs and not bank_objs:
        raise HTTPException(400, "Nothing to reconcile — upload the dump and a statement first.")
    # SRC-disposed rows are excluded from re-matching and left untouched (parity with
    # core run_reconciliation) so a manual SRC tag survives a recon re-run.
    internal = [{"_db": o, "eko_trxn_id": o.eko_trxn_id, "amount": float(o.amount or 0),
                 "is_refunded": bool(o.is_refunded), "status": o.status, "provider": o.provider}
                for o in int_objs if o.recon_status != "src_assigned"]
    bank = [{"_db": o, "provider": o.provider, "client_ref": o.client_ref, "amount": float(o.amount or 0),
             "status": o.status} for o in bank_objs if o.recon_status != "src_assigned"]
    res = BB.reconcile(internal, bank)
    for d in res["internal_rows"]:
        o = d["_db"]; o.recon_status = d["recon_status"]; o.match_id = d.get("match_id", ""); o.match_note = (d.get("match_note") or "")[:300]
    for d in res["bank_rows"]:
        o = d["_db"]; o.recon_status = d["recon_status"]; o.match_id = d.get("match_id", ""); o.match_note = (d.get("match_note") or "")[:300]
    _audit(db, user, "bbps_run_recon", res["summary"])
    db.commit()
    return res["summary"]


# ─── Summary / results ────────────────────────────────────────────────────────
def _date_filter(q, col, date_from, date_to):
    if date_from: q = q.filter(col >= date_from)
    if date_to:   q = q.filter(col <= date_to)
    return q


@router.get("/summary")
def summary(date_from: str = Query(""), date_to: str = Query(""),
            db: Session = Depends(get_db), user=Depends(get_current_user)):
    # PERF: aggregate in SQL (GROUP BY) instead of loading entire tables.
    from sqlalchemy import func
    from core.dash_cache import dash_key, dash_get, dash_put
    _ck = dash_key("bbps_summary", db, date_from=date_from, date_to=date_to)
    _hit = dash_get(_ck)
    if _hit is not None:
        return _hit
    STAT = ["matched", "failed_refunded", "failed_pending_refund", "refunded_but_success",
            "amount_mismatch", "unmatched_bank"]

    bq = db.query(BbpsBankTxn.provider, BbpsBankTxn.recon_status, func.count(BbpsBankTxn.id))
    bq = _date_filter(bq, BbpsBankTxn.transaction_date, date_from, date_to)
    bank_counts = bq.group_by(BbpsBankTxn.provider, BbpsBankTxn.recon_status).all()

    iq = db.query(BbpsInternal.recon_status, func.count(BbpsInternal.id))
    iq = _date_filter(iq, BbpsInternal.transaction_date, date_from, date_to)
    internal_counts = iq.group_by(BbpsInternal.recon_status).all()

    def rollup(provider=None):
        rel = [(p, s, c) for (p, s, c) in bank_counts if provider is None or p == provider]
        c = {s: 0 for s in STAT}
        total = 0
        for (_p, s, cnt) in rel:
            total += cnt
            if s in c:
                c[s] += cnt
        recon = c["matched"] + c["failed_refunded"]
        return {**c, "bank_rows": total, "reconciled": recon,
                "match_rate": round(recon / (total or 1) * 100, 1)}

    providers = sorted({p for (p, _s, _c) in bank_counts if p})
    internal_rows = sum(c for (_s, c) in internal_counts)
    unmatched_internal = sum(c for (s, c) in internal_counts if s == "unmatched_internal")
    exceptions = sum(c for (_p, s, c) in bank_counts
                     if s in ("failed_pending_refund", "refunded_but_success", "amount_mismatch"))
    return dash_put(_ck, {
        "overall": {**rollup(), "internal_rows": internal_rows,
                    "unmatched_internal": unmatched_internal,
                    "exceptions": exceptions},
        "by_provider": [{"provider": p, **rollup(p)} for p in providers],
    })


@router.get("/results")
def results(side: str = Query("bank", pattern="^(bank|internal)$"), status: str = Query(""),
            provider: str = Query(""), date_from: str = Query(""), date_to: str = Query(""),
            db: Session = Depends(get_db), user=Depends(get_current_user)):
    if side == "bank":
        q = db.query(BbpsBankTxn)
        if status:   q = q.filter(BbpsBankTxn.recon_status == status)
        if provider: q = q.filter(BbpsBankTxn.provider == provider)
        q = _date_filter(q, BbpsBankTxn.transaction_date, date_from, date_to)
        rows = q.order_by(BbpsBankTxn.transaction_date).all()
        return [{"id": b.id, "provider": b.provider, "client_ref": b.client_ref, "operator_ref": b.operator_ref,
                 "amount": b.amount, "status": b.status, "transaction_date": b.transaction_date,
                 "service_name": b.service_name, "operator_name": b.operator_name, "reason": b.reason,
                 "recon_status": b.recon_status, "match_id": b.match_id, "match_note": b.match_note,
                 "src_code": b.src_code, "src_note": b.src_note} for b in rows]
    q = db.query(BbpsInternal)
    if status:   q = q.filter(BbpsInternal.recon_status == status)
    if provider: q = q.filter(BbpsInternal.provider == provider)
    q = _date_filter(q, BbpsInternal.transaction_date, date_from, date_to)
    rows = q.order_by(BbpsInternal.transaction_date).all()
    return [{"id": i.id, "eko_trxn_id": i.eko_trxn_id, "provider": i.provider, "amount": i.amount,
             "status": i.status, "is_refunded": i.is_refunded, "transaction_date": i.transaction_date,
             "csp_code": i.csp_code, "merchant_name": i.merchant_name, "cell_number": i.cell_number,
             "recon_status": i.recon_status, "match_id": i.match_id, "match_note": i.match_note,
             "src_code": i.src_code, "src_note": i.src_note} for i in rows]


# ─── Export ───────────────────────────────────────────────────────────────────
@router.get("/export")
def export(date_from: str = Query(""), date_to: str = Query(""),
           db: Session = Depends(get_db), user=Depends(get_current_user)):
    bank = _date_filter(db.query(BbpsBankTxn), BbpsBankTxn.transaction_date, date_from, date_to) \
        .order_by(BbpsBankTxn.provider, BbpsBankTxn.transaction_date).all()
    internal = _date_filter(db.query(BbpsInternal), BbpsInternal.transaction_date, date_from, date_to) \
        .order_by(BbpsInternal.transaction_date).all()
    bdf = pd.DataFrame([{"Provider": b.provider, "Client Ref": b.client_ref, "Operator Ref": b.operator_ref,
                         "Amount": b.amount, "Status": b.status, "Date": b.transaction_date,
                         "Service": b.service_name, "Operator": b.operator_name, "Reason": b.reason,
                         "Recon Status": b.recon_status, "Match ID": b.match_id, "Note": b.match_note} for b in bank])
    idf = pd.DataFrame([{"Eko TID": i.eko_trxn_id, "Provider": i.provider, "Amount": i.amount,
                         "Status": i.status, "Refunded": i.is_refunded, "Date": i.transaction_date,
                         "CSP": i.csp_code, "Merchant": i.merchant_name, "Mobile": i.cell_number,
                         "Recon Status": i.recon_status, "Match ID": i.match_id, "Note": i.match_note} for i in internal])
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        (bdf if not bdf.empty else pd.DataFrame({"info": ["no operator rows"]})).to_excel(w, index=False, sheet_name="Operator")
        (idf if not idf.empty else pd.DataFrame({"info": ["no internal rows"]})).to_excel(w, index=False, sheet_name="Internal")
        from openpyxl.styles import PatternFill, Font as XF
        for sh in w.sheets.values():
            for col in sh.columns:
                m = max((len(str(c.value or "")) for c in col), default=10)
                sh.column_dimensions[col[0].column_letter].width = min(m + 2, 44)
            for c in sh[1]:
                c.fill = PatternFill("solid", fgColor="1E3A5F"); c.font = XF(bold=True, color="FFFFFF")
    out.seek(0)
    return StreamingResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="bbps_recon_{_TODAY()}.xlsx"'})


# ─── Manual fixes ─────────────────────────────────────────────────────────────
class ManualMatchIn(BaseModel):
    bank_id: str
    internal_id: str


@router.post("/manual-match")
def manual_match(body: ManualMatchIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    b = db.query(BbpsBankTxn).filter(BbpsBankTxn.id == body.bank_id).first()
    i = db.query(BbpsInternal).filter(BbpsInternal.id == body.internal_id).first()
    if not b or not i:
        raise HTTPException(404, "Operator or internal row not found")
    queued = maker_checker.intercept(
        db, user, "bbps_manual_match",
        payload={"bank_id": body.bank_id, "internal_id": body.internal_id},
        summary=f"BBPS manual match: bank {body.bank_id} <-> internal {body.internal_id}",
        partner="bbps")
    if queued:
        return queued
    mid = f"BBPSMAN-{generate_id()[:6].upper()}"
    status = BB._classify({"amount": b.amount, "status": b.status},
                          {"amount": i.amount, "is_refunded": i.is_refunded})
    for o in (b, i):
        o.recon_status = status; o.match_id = mid; o.match_note = "manual match"
    _audit(db, user, "bbps_manual_match", {"bank": b.id, "internal": i.id, "status": status})
    db.commit()
    return {"message": "Matched", "match_id": mid, "status": status}


@router.post("/unmatch")
def unmatch(match_id: str = Query(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    queued = maker_checker.intercept(
        db, user, "bbps_unmatch",
        payload={"match_id": match_id},
        summary=f"BBPS unmatch: {match_id}",
        partner="bbps")
    if queued:
        return queued
    n = 0
    for b in db.query(BbpsBankTxn).filter(BbpsBankTxn.match_id == match_id).all():
        b.recon_status = "unmatched_bank"; b.match_id = ""; b.match_note = "unmatched"; n += 1
    for i in db.query(BbpsInternal).filter(BbpsInternal.match_id == match_id).all():
        i.recon_status = "unmatched_internal"; i.match_id = ""; i.match_note = "unmatched"; n += 1
    _audit(db, user, "bbps_unmatch", {"match_id": match_id, "rows_reset": n})
    db.commit()
    return {"message": "Unmatched", "rows_reset": n}


class OverrideIn(BaseModel):
    id: str
    side: str          # bank | internal
    status: str
    note: str


@router.post("/override-status")
def override_status(body: OverrideIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if len((body.note or "").strip()) < 10:
        raise HTTPException(422, "A remark of at least 10 characters is required")
    if body.status not in OVERRIDE_STATUSES:
        raise HTTPException(400, f"Invalid status. Allowed: {OVERRIDE_STATUSES}")
    Model = BbpsBankTxn if body.side == "bank" else BbpsInternal
    row = db.query(Model).filter(Model.id == body.id).first()
    if not row:
        raise HTTPException(404, "Row not found")
    queued = maker_checker.intercept(
        db, user, "bbps_override",
        payload={"id": body.id, "side": body.side, "status": body.status, "note": body.note},
        summary=f"BBPS override {body.side} {body.id} -> {body.status}",
        partner="bbps")
    if queued:
        return queued
    prev = {"recon_status": row.recon_status}
    row.prev_recon_status = row.recon_status
    row.recon_status = body.status
    row.match_note = f"override: {body.note.strip()}"
    row.override_by = getattr(user, "username", None)
    row.override_at = datetime.datetime.utcnow()
    _audit(db, user, "bbps_override_status", {"id": body.id, "side": body.side, "to": body.status,
                                              "note": body.note}, entity_id=body.id, prev=prev)
    db.commit()
    return {"message": "Status updated", "status": body.status}


# ── SRC disposition (parity with core-ledger /recon/assign-src) ────────────────
# Tags an unmatched BBPS row with a source code + note → recon_status='src_assigned'.
# Same access level as override-status; preserved across re-runs by run_recon.
class BbpsSRCIn(BaseModel):
    id: str
    side: str          # "bank" | "internal"
    src_code: str
    src_note: str = ""


class BbpsBulkSRCIn(BaseModel):
    ids: List[str]
    side: str
    src_code: str
    src_note: str = ""


@router.post("/assign-src")
def assign_src(body: BbpsSRCIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Assign an SRC code + note to one BBPS row (operator/bank or internal)."""
    from routes.recon import is_valid_src_code
    if not is_valid_src_code(db, body.src_code):
        raise HTTPException(400, f"Invalid SRC code: {body.src_code}")
    Model = BbpsBankTxn if body.side == "bank" else BbpsInternal
    row = db.query(Model).filter(Model.id == body.id).first()
    if not row:
        raise HTTPException(404, "Row not found")
    queued = maker_checker.intercept(
        db, user, "bbps_assign_src",
        payload={"id": body.id, "side": body.side, "src_code": body.src_code, "src_note": body.src_note},
        summary=f"BBPS SRC {body.src_code} -> {body.side} {body.id}",
        partner="bbps")
    if queued:
        return queued
    prev = {"recon_status": row.recon_status, "src_code": row.src_code, "match_id": row.match_id}
    row.prev_recon_status = row.recon_status
    row.recon_status = "src_assigned"
    row.src_code = body.src_code
    row.src_note = (body.src_note or "")[:500]
    row.override_by = getattr(user, "username", None)
    row.override_at = datetime.datetime.utcnow()
    _audit(db, user, "bbps_assign_src",
           {"id": body.id, "side": body.side, "src_code": body.src_code, "src_note": body.src_note},
           entity_id=body.id, prev=prev)
    db.commit()
    return {"message": "SRC assigned", "id": body.id, "src_code": body.src_code}


@router.post("/assign-src-bulk")
def assign_src_bulk(body: BbpsBulkSRCIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Assign one SRC code + note to many BBPS rows in one shot."""
    from routes.recon import is_valid_src_code
    if not is_valid_src_code(db, body.src_code):
        raise HTTPException(400, f"Invalid SRC code: {body.src_code}")
    Model = BbpsBankTxn if body.side == "bank" else BbpsInternal
    rows = db.query(Model).filter(Model.id.in_(body.ids or [])).all()
    queued = maker_checker.intercept(
        db, user, "bbps_assign_src_bulk",
        payload={"ids": body.ids, "side": body.side, "src_code": body.src_code, "src_note": body.src_note},
        summary=f"BBPS bulk SRC {body.src_code} -> {len(body.ids or [])} {body.side} rows",
        partner="bbps")
    if queued:
        return queued
    for row in rows:
        row.prev_recon_status = row.recon_status
        row.recon_status = "src_assigned"
        row.src_code = body.src_code
        row.src_note = (body.src_note or "")[:500]
        row.override_by = getattr(user, "username", None)
        row.override_at = datetime.datetime.utcnow()
    _audit(db, user, "bbps_assign_src_bulk", {"side": body.side, "src_code": body.src_code, "count": len(rows)})
    db.commit()
    return {"updated": len(rows), "src_code": body.src_code}


class BbpsSRCRemoveIn(BaseModel):
    id: str
    side: str            # bank | internal


class BbpsBulkSRCRemoveIn(BaseModel):
    ids: list
    side: str


def _bbps_open_default(side):
    return "unmatched_bank" if side == "bank" else "unmatched_internal"


@router.post("/remove-src")
def remove_src(body: BbpsSRCRemoveIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Remove the SRC tag from one BBPS row and revert it to the status it held before the tag
    (falls back to unmatched_bank / unmatched_internal for pre-existing rows)."""
    Model = BbpsBankTxn if body.side == "bank" else BbpsInternal
    row = db.query(Model).filter(Model.id == body.id).first()
    if not row:
        raise HTTPException(404, "Row not found")
    if row.recon_status != "src_assigned":
        raise HTTPException(400, "Row is not SRC-tagged")
    queued = maker_checker.intercept(
        db, user, "bbps_remove_src",
        payload={"id": body.id, "side": body.side},
        summary=f"BBPS remove SRC -> {body.side} {body.id}", partner="bbps")
    if queued:
        return queued
    prev = row.prev_recon_status
    if not prev or prev == "src_assigned":
        prev = _bbps_open_default(body.side)
    old_code = row.src_code
    row.recon_status = prev
    row.src_code = None
    row.src_note = None
    row.prev_recon_status = None
    row.override_by = getattr(user, "username", None)
    row.override_at = datetime.datetime.utcnow()
    _audit(db, user, "bbps_remove_src",
           {"id": body.id, "side": body.side, "reverted_to": prev, "removed_src_code": old_code},
           entity_id=body.id)
    db.commit()
    return {"message": "SRC removed", "id": body.id, "reverted_to": prev}


@router.post("/remove-src-bulk")
def remove_src_bulk(body: BbpsBulkSRCRemoveIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Remove SRC tags from many BBPS rows; each reverts to its own prior status."""
    Model = BbpsBankTxn if body.side == "bank" else BbpsInternal
    rows = db.query(Model).filter(Model.id.in_(body.ids or []),
                                  Model.recon_status == "src_assigned").all()
    queued = maker_checker.intercept(
        db, user, "bbps_remove_src_bulk",
        payload={"ids": body.ids, "side": body.side},
        summary=f"BBPS remove SRC -> {len(body.ids or [])} {body.side} rows", partner="bbps")
    if queued:
        return queued
    default = _bbps_open_default(body.side)
    for row in rows:
        prev = row.prev_recon_status
        if not prev or prev == "src_assigned":
            prev = default
        row.recon_status = prev
        row.src_code = None
        row.src_note = None
        row.prev_recon_status = None
        row.override_by = getattr(user, "username", None)
        row.override_at = datetime.datetime.utcnow()
    _audit(db, user, "bbps_remove_src_bulk", {"side": body.side, "count": len(rows)})
    db.commit()
    return {"reverted": len(rows), "side": body.side}
