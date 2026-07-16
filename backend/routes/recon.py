import logging
import json
import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel
from models.database import (
    get_db, Transaction, ReconRun, MatchRule, ReconStatus, User,
    AuditLog, generate_id
)
from core.auth import get_current_user, require_permission, check_product_access
from core import maker_checker
from core.matching_engine import run_reconciliation, run_neft_d1_match, run_internal_match, assign_src, manual_match

logger = logging.getLogger("eko_recon")

router = APIRouter(prefix="/api/recon", tags=["reconciliation"])

SRC_CODES = [
    "UNCLAIMED", "ADVANCE_CREDIT", "BANK_CHARGES",
    "TWICE_CREDITED", "INTERNAL_TXN", "DELAYED_TXN",
    "DUPLICATE", "MISSING_TID", "OTHER"
]

# ── Pydantic request schemas ───────────────────────────────────────────────────
class RunReconRequest(BaseModel):
    partner: str
    recon_date: str

class SRCRequest(BaseModel):
    transaction_id: str
    src_code: str
    src_note: Optional[str] = ""

class BulkSRCRequest(BaseModel):
    transaction_ids: List[str]
    src_code: str
    src_note: Optional[str] = ""

class ManualMatchRequest(BaseModel):
    bank_txn_id: str
    internal_txn_id: str

class RunRangeRequest(BaseModel):
    partner: str
    date_from: str
    date_to: str

class RuleCreate(BaseModel):
    name: str
    partner: str
    priority: int = 1
    match_fields: List[str]
    description: Optional[str] = ""

# ── Helper ─────────────────────────────────────────────────────────────────────
def _log(db: Session, user: User, action: str, entity_type: str = None,
         entity_id: str = None, detail: dict = None, action_type: str = "human"):
    """
    Append an audit log entry — fire-and-forget, never raises.
    action_type='human' (default) = triggered by a real user request.
    action_type='app'   = triggered by automated scheduler / system.
    """
    try:
        db.add(AuditLog(
            user_id=user.id,
            username=user.username,
            action=action,
            action_type=action_type,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=json.dumps(detail) if detail else None,
        ))
        db.commit()
    except Exception:
        db.rollback()


# ── Core endpoints ─────────────────────────────────────────────────────────────
@router.post("/run")
def run_recon(req: RunReconRequest, db: Session = Depends(get_db),
              current_user: User = Depends(require_permission("run_recon"))):
    check_product_access(db, current_user, req.partner)
    result = run_reconciliation(req.partner, req.recon_date, db, current_user.id)
    # Cross-date RRN pass (QR T+1): pairs split across recon dates — scoped partners only.
    try:
        from core.matching_engine import run_cross_date_rrn_match, CROSS_DATE_RRN_PARTNERS
        if req.partner in CROSS_DATE_RRN_PARTNERS:
            result.update(run_cross_date_rrn_match(req.partner, db, current_user.id))
    except Exception:
        pass
    _log(db, current_user, "run_recon", "recon_run", detail={
        "partner": req.partner, "recon_date": req.recon_date,
        "matched": result.get("matched"), "unmatched_bank": result.get("unmatched_bank"),
        "unmatched_internal": result.get("unmatched_internal"),
    })
    # Fire low-match-rate alert if needed (non-blocking)
    try:
        from core.notifications import send_match_rate_alert, ALERT_THRESHOLD
        total_bank = (result.get("matched", 0) or 0) + (result.get("unmatched_bank", 0) or 0)
        if total_bank > 0:
            rate = round((result.get("matched", 0) or 0) / total_bank * 100, 1)
            result["match_rate"] = rate
            if rate < ALERT_THRESHOLD:
                send_match_rate_alert(
                    req.partner, req.recon_date, rate,
                    result.get("matched", 0), result.get("unmatched_bank", 0)
                )
    except Exception:
        pass
    return result

@router.post("/run-range")
def run_recon_range(req: RunRangeRequest, db: Session = Depends(get_db),
                    current_user: User = Depends(require_permission("run_recon"))):
    """Run reconciliation for every uploaded date in the specified range."""
    check_product_access(db, current_user, req.partner)
    from sqlalchemy import distinct as sqldistinct
    dates = db.query(Transaction.recon_date).filter(
        Transaction.partner == req.partner,
        Transaction.recon_date >= req.date_from,
        Transaction.recon_date <= req.date_to,
        Transaction.recon_date.isnot(None)
    ).distinct().order_by(Transaction.recon_date).all()
    dates = [d[0] for d in dates if d[0]]

    results = []
    total_matched = 0
    for date in dates:
        r = run_reconciliation(req.partner, date, db, current_user.id)
        total_matched += r.get("matched", 0)
        results.append({"date": date, "matched": r.get("matched", 0),
                        "unmatched_bank": r.get("unmatched_bank", 0),
                        "unmatched_internal": r.get("unmatched_internal", 0)})
    _log(db, current_user, "run_recon_range", detail={
        "partner": req.partner, "date_from": req.date_from, "date_to": req.date_to,
        "dates_processed": len(dates), "total_matched": total_matched
    })
    return {"dates_processed": len(dates), "total_matched": total_matched, "results": results}

@router.post("/run-neft-d1")
def run_neft(req: RunReconRequest, db: Session = Depends(get_db),
             current_user: User = Depends(require_permission("run_recon"))):
    result = run_neft_d1_match(req.partner, req.recon_date, db, current_user.id)
    _log(db, current_user, "run_neft_d1", "recon_run", detail=result)
    return result

@router.post("/run-internal-match")
def run_internal_match_route(req: RunReconRequest, db: Session = Depends(get_db),
                             current_user: User = Depends(require_permission("run_recon"))):
    """
    Internal-to-Internal matching:
    Finds Success + Refunded/Failed pairs with the same TID in the same dump
    and marks both rows as internal_matched (they cancel each other out).
    """
    result = run_internal_match(req.partner, req.recon_date, db, current_user.id)
    _log(db, current_user, "run_internal_match", "recon_run", detail={
        "partner": req.partner, "recon_date": req.recon_date,
        "internal_matched": result.get("internal_matched"),
    })
    return result

@router.post("/run-interbank-match")
def run_interbank_match_route(db: Session = Depends(get_db),
                              current_user: User = Depends(require_permission("run_recon"))):
    """
    Cross-bank fund movement matching.
    Finds all fund_transfer rows (bank-side) that have a UTR number and matches
    CR entries against DR entries across ALL partners/banks using UTR.
    No date or partner filter — runs across the entire dataset.
    """
    import traceback
    try:
        from core.matching_engine import run_interbank_match
        result = run_interbank_match(db, current_user.id)
        _log(db, current_user, "interbank_match", "transaction", detail=result)
        return result
    except Exception as e:
        tb = traceback.format_exc()
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}\n{tb}")


# ── Pydantic schemas for new human-action endpoints ───────────────────────────

class TagAdhocRequest(BaseModel):
    transaction_id: str
    remark: str            # MANDATORY — rejected if empty

class OverrideStatusRequest(BaseModel):
    transaction_id: str
    new_status: str        # target status
    remark: str            # MANDATORY — rejected if empty

class UnmatchRequest(BaseModel):
    match_id: str          # break this match (both transactions go back to unmatched)
    remark: str            # MANDATORY — rejected if empty

class RematchRequest(BaseModel):
    bank_txn_id: str       # bank-side transaction to re-match
    new_internal_txn_id: str  # the correct internal transaction to pair with
    remark: str            # MANDATORY — rejected if empty

class ManualInterbankRequest(BaseModel):
    txn_id_a: str          # first fund_transfer transaction (any bank)
    txn_id_b: str          # second fund_transfer transaction (different bank)
    remark: str            # MANDATORY — rejected if empty

class BulkOverrideRequest(BaseModel):
    transaction_ids: List[str]  # list of transaction IDs to override
    new_status: str             # target status
    remark: str                 # MANDATORY — one remark applies to all

class BulkManualMatchRequest(BaseModel):
    pairs: List[dict]  # list of {bank_txn_id, internal_txn_id} dicts


REMARK_MIN_LEN = 10

def _require_remark(remark: str, action: str):
    """Server-side enforcement: a meaningful remark (>= 10 chars) is mandatory for
    all human override actions, so overrides are explained, not just clicked through."""
    cleaned = (remark or "").strip()
    if not cleaned:
        raise HTTPException(
            status_code=422,
            detail=f"Remark is required for '{action}' — entry cannot be saved without a remark."
        )
    if len(cleaned) < REMARK_MIN_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"Remark for '{action}' must be at least {REMARK_MIN_LEN} characters — "
                   f"please explain the reason (got {len(cleaned)})."
        )

def _human_log(db: Session, user: User, action: str, txn_id: str,
               prev_status: str, new_status: str, remark: str, extra: dict = None):
    """
    Audit log entry specifically for human override actions.
    action_type='human' makes these distinguishable from app actions in the audit log.
    previous_state captures the full before-snapshot for end-to-end traceability.
    """
    import json as _json, datetime as _dt
    prev = _json.dumps({"recon_status": prev_status, **(extra or {})})
    try:
        db.add(AuditLog(
            id=generate_id(), user_id=user.id, username=user.username,
            action=action, action_type="human",
            entity_type="transaction", entity_id=txn_id,
            detail=_json.dumps({"new_status": new_status, "remark": remark, **(extra or {})}),
            previous_state=prev,
        ))
        db.commit()
    except Exception:
        db.rollback()


@router.post("/tag-adhoc")
def tag_adhoc_settlement(req: TagAdhocRequest,
                         db: Session = Depends(get_db),
                         current_user: User = Depends(require_permission("src_assign"))):
    """
    Tag a bank-side unmatched transaction as an Adhoc Settlement to a CSP/SCSP.
    These are external fund transfers that will never appear in Simplibank.
    Remark is MANDATORY — the action is rejected server-side if remark is empty.
    Full audit trail: human action logged separately from app actions.
    """
    _require_remark(req.remark, "tag_adhoc")

    txn = db.query(Transaction).filter(Transaction.id == req.transaction_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if txn.side != "bank":
        raise HTTPException(status_code=400,
                            detail="Adhoc settlement can only be tagged on bank-side transactions.")

    prev_status = txn.recon_status

    txn.recon_status    = ReconStatus.adhoc_settlement
    txn.src_note        = req.remark
    txn.override_note   = req.remark
    txn.override_by     = current_user.username
    txn.override_at     = datetime.datetime.utcnow()
    txn.prev_recon_status = str(prev_status)
    db.commit()

    _human_log(db, current_user, "tag_adhoc", txn.id,
               str(prev_status), "adhoc_settlement", req.remark)
    return {"message": "Tagged as adhoc settlement", "transaction_id": txn.id,
            "previous_status": str(prev_status), "new_status": "adhoc_settlement"}


@router.post("/override-status")
def override_status(req: OverrideStatusRequest,
                    db: Session = Depends(get_db),
                    current_user: User = Depends(require_permission("override"))):
    """
    Human override: change a single transaction's recon status.
    Allowed target statuses: unmatched, failed, src_assigned, adhoc_settlement, human_override.
    Remark is MANDATORY. Cannot be used to mark as 'matched' — use rematch for that.
    Full audit trail with action_type='human' and previous_state snapshot.
    """
    _require_remark(req.remark, "override_status")

    # Maker-checker: non-admin actions queue for approval when enabled
    queued = maker_checker.intercept(
        db, current_user, "override",
        payload={"transaction_id": req.transaction_id, "new_status": req.new_status, "remark": req.remark},
        summary=f"Override txn {req.transaction_id} → {req.new_status} ({req.remark[:80]})",
    )
    if queued:
        return queued

    ALLOWED_TARGETS = {
        "unmatched", "failed", "src_assigned", "adhoc_settlement", "human_override"
    }
    if req.new_status not in ALLOWED_TARGETS:
        raise HTTPException(status_code=400,
            detail=f"Cannot override to '{req.new_status}'. "
                   f"Allowed: {sorted(ALLOWED_TARGETS)}. "
                   f"To match transactions use /rematch.")

    txn = db.query(Transaction).filter(Transaction.id == req.transaction_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    prev_status = txn.recon_status
    prev_match  = txn.match_id

    # If breaking out of a match — clear both sides
    if str(prev_status) in ("matched", "manual_matched", "interbank_matched") and txn.match_id:
        # Find and clear the other side of the match
        other = db.query(Transaction).filter(
            Transaction.match_id == txn.match_id,
            Transaction.id != txn.id
        ).first()
        if other:
            other.recon_status    = ReconStatus.unmatched
            other.match_id        = None
            other.matched_with_id = None
            other.override_note   = f"Match broken by {current_user.username}: {req.remark}"
            other.override_by     = current_user.username
            other.override_at     = datetime.datetime.utcnow()
            other.prev_recon_status = str(prev_status)
            _human_log(db, current_user, "override_break_match_other", other.id,
                       str(prev_status), "unmatched", req.remark,
                       {"match_id_broken": prev_match})

    txn.recon_status    = req.new_status
    txn.match_id        = None if req.new_status == "unmatched" else txn.match_id
    txn.matched_with_id = None if req.new_status == "unmatched" else txn.matched_with_id
    txn.override_note   = req.remark
    txn.override_by     = current_user.username
    txn.override_at     = datetime.datetime.utcnow()
    txn.prev_recon_status = str(prev_status)
    if req.new_status == "src_assigned":
        txn.src_note = req.remark
    db.commit()

    _human_log(db, current_user, "override_status", txn.id,
               str(prev_status), req.new_status, req.remark,
               {"match_id_broken": prev_match})
    return {"message": "Status overridden", "transaction_id": txn.id,
            "previous_status": str(prev_status), "new_status": req.new_status}


@router.post("/bulk-override-status")
def bulk_override_status(
    req: BulkOverrideRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("override")),
):
    """
    Override multiple transactions' recon status in one operation.
    Same rules as single override — remark is MANDATORY, each transaction
    gets its own audit trail entry. Returns count of updated rows.
    """
    _require_remark(req.remark, "bulk_override")

    queued = maker_checker.intercept(
        db, current_user, "bulk_override",
        payload={"transaction_ids": req.transaction_ids, "new_status": req.new_status, "remark": req.remark},
        summary=f"Bulk override {len(req.transaction_ids)} rows → {req.new_status}",
    )
    if queued:
        return queued

    ALLOWED_TARGETS = {"unmatched", "failed", "src_assigned", "adhoc_settlement", "human_override"}
    if req.new_status not in ALLOWED_TARGETS:
        raise HTTPException(status_code=400,
            detail=f"Cannot override to '{req.new_status}'. Allowed: {sorted(ALLOWED_TARGETS)}.")

    if not req.transaction_ids:
        raise HTTPException(status_code=400, detail="No transaction IDs provided")

    txns = db.query(Transaction).filter(Transaction.id.in_(req.transaction_ids)).all()
    if not txns:
        raise HTTPException(status_code=404, detail="No matching transactions found")

    updated = 0
    for txn in txns:
        prev_status = txn.recon_status
        prev_match  = txn.match_id

        # Break any existing match on the other side
        if str(prev_status) in ("matched", "manual_matched", "interbank_matched") and txn.match_id:
            others = db.query(Transaction).filter(
                Transaction.match_id == txn.match_id,
                Transaction.id != txn.id
            ).all()
            for other in others:
                prev_o = other.recon_status
                other.recon_status      = ReconStatus.unmatched
                other.match_id          = None
                other.matched_with_id   = None
                other.override_note     = f"Bulk override by {current_user.username}: {req.remark}"
                other.override_by       = current_user.username
                other.override_at       = datetime.datetime.utcnow()
                other.prev_recon_status = str(prev_o)
                _human_log(db, current_user, "bulk_override_break_match", other.id,
                           str(prev_o), "unmatched", req.remark, {"match_id_broken": prev_match})

        txn.recon_status      = req.new_status
        txn.match_id          = None if req.new_status == "unmatched" else txn.match_id
        txn.matched_with_id   = None if req.new_status == "unmatched" else txn.matched_with_id
        txn.override_note     = req.remark
        txn.override_by       = current_user.username
        txn.override_at       = datetime.datetime.utcnow()
        txn.prev_recon_status = str(prev_status)
        _human_log(db, current_user, "bulk_override", txn.id,
                   str(prev_status), req.new_status, req.remark)
        updated += 1

    db.commit()
    return {"message": f"Bulk override applied to {updated} transaction(s)",
            "updated": updated, "new_status": req.new_status}


@router.post("/unmatch")
def unmatch_pair(req: UnmatchRequest,
                 db: Session = Depends(get_db),
                 current_user: User = Depends(require_permission("override"))):
    """
    Break an existing match pair (both sides go back to unmatched).
    Remark is MANDATORY — rejected without it.
    Both transactions are individually logged in the audit trail as human actions.
    """
    _require_remark(req.remark, "unmatch")

    # Maker-checker: non-admin unmatch queues for approval when enabled
    queued = maker_checker.intercept(
        db, current_user, "unmatch",
        payload={"match_id": req.match_id, "remark": req.remark},
        summary=f"Unmatch {req.match_id} ({req.remark[:80]})",
    )
    if queued:
        return queued

    txns = db.query(Transaction).filter(Transaction.match_id == req.match_id).all()
    if not txns:
        raise HTTPException(status_code=404, detail=f"No transactions found with match_id '{req.match_id}'")

    for txn in txns:
        prev_status = txn.recon_status
        txn.recon_status    = ReconStatus.unmatched
        txn.match_id        = None
        txn.matched_with_id = None
        txn.override_note   = req.remark
        txn.override_by     = current_user.username
        txn.override_at     = datetime.datetime.utcnow()
        txn.prev_recon_status = str(prev_status)
        _human_log(db, current_user, "human_unmatch", txn.id,
                   str(prev_status), "unmatched", req.remark,
                   {"match_id_broken": req.match_id})

    db.commit()
    return {"message": f"Match '{req.match_id}' broken — {len(txns)} transactions set to unmatched",
            "match_id": req.match_id, "affected": len(txns)}


@router.post("/rematch")
def rematch_transactions(req: RematchRequest,
                         db: Session = Depends(get_db),
                         current_user: User = Depends(require_permission("override"))):
    """
    Break any existing match on bank_txn_id and re-match it with a new internal transaction.
    Steps:
      1. If bank_txn is currently matched → break that match (other side → unmatched)
      2. If new_internal_txn is currently matched → break that match (other side → unmatched)
      3. Create a new manual_matched pair between the two
    Remark is MANDATORY for the full override trail.
    """
    _require_remark(req.remark, "rematch")

    bank_txn = db.query(Transaction).filter(Transaction.id == req.bank_txn_id).first()
    new_int  = db.query(Transaction).filter(Transaction.id == req.new_internal_txn_id).first()
    if not bank_txn or not new_int:
        raise HTTPException(status_code=404, detail="One or both transactions not found")

    def _break_existing(txn):
        if txn.match_id:
            others = db.query(Transaction).filter(
                Transaction.match_id == txn.match_id,
                Transaction.id != txn.id
            ).all()
            prev_mid = txn.match_id
            for o in others:
                prev = o.recon_status
                o.recon_status    = ReconStatus.unmatched
                o.match_id        = None
                o.matched_with_id = None
                o.override_note   = f"Re-match by {current_user.username}: {req.remark}"
                o.override_by     = current_user.username
                o.override_at     = datetime.datetime.utcnow()
                o.prev_recon_status = str(prev)
                _human_log(db, current_user, "rematch_break_other", o.id,
                           str(prev), "unmatched", req.remark,
                           {"match_id_broken": prev_mid})

    _break_existing(bank_txn)
    _break_existing(new_int)

    # Build new match
    from core.matching_engine import _next_seq, _get_prefix
    p = bank_txn.partner or "manual"
    d = bank_txn.recon_date or ""
    seq = _next_seq(p, d, db)
    from core.matching_engine import _make_match_id
    mid = _make_match_id(p, d, seq)

    # Amount guard — parity with manual_match()/the auto-matcher: beyond ±₹1 the
    # re-matched pair is flagged `amount_mismatch` (still linked, remark preserved)
    # rather than presented as a clean match.
    amt_diff = abs(float(bank_txn.amount or 0) - float(new_int.amount or 0))
    final_status = ReconStatus.amount_mismatch if amt_diff > 1.0 else ReconStatus.manual_matched

    for txn, partner_id in [(bank_txn, new_int.id), (new_int, bank_txn.id)]:
        prev = txn.recon_status
        txn.recon_status    = final_status
        txn.match_id        = mid
        txn.matched_with_id = partner_id
        txn.override_note   = req.remark
        txn.override_by     = current_user.username
        txn.override_at     = datetime.datetime.utcnow()
        txn.prev_recon_status = str(prev)
        _human_log(db, current_user, "human_rematch", txn.id,
                   str(prev), final_status.value, req.remark,
                   {"new_match_id": mid, "partner_txn_id": partner_id})

    db.commit()
    return {"message": "Re-matched successfully", "new_match_id": mid, "status": final_status.value,
            "amount_diff": round(amt_diff, 2), "amount_mismatch": amt_diff > 1.0,
            "bank_txn_id": req.bank_txn_id, "internal_txn_id": req.new_internal_txn_id}


@router.get("/audit-trail/{transaction_id}")
def get_transaction_audit_trail(
    transaction_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Full end-to-end audit trail for a single transaction.
    Returns all audit events — app and human — ordered by time.
    action_type='app' = system did this | action_type='human' = person did this.
    """
    events = db.query(AuditLog).filter(
        AuditLog.entity_id == transaction_id
    ).order_by(AuditLog.created_at.asc()).all()

    return {
        "transaction_id": transaction_id,
        "events": [
            {
                "timestamp":      str(e.created_at),
                "action_type":    getattr(e, 'action_type', 'app'),
                "action":         e.action,
                "actor":          e.username,
                "detail":         json.loads(e.detail) if e.detail else {},
                "previous_state": json.loads(e.previous_state) if getattr(e, 'previous_state', None) else None,
            }
            for e in events
        ],
        "app_actions":   sum(1 for e in events if getattr(e, 'action_type', 'app') == 'app'),
        "human_actions": sum(1 for e in events if getattr(e, 'action_type', 'app') == 'human'),
    }


@router.get("/interbank-matches")
def list_interbank_matches(
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    partner:   Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all interbank fund movement matches (IBT- match IDs)."""
    q = db.query(Transaction).filter(
        Transaction.recon_status == ReconStatus.interbank_matched,
        Transaction.match_id.like("IBT-%")
    )
    if date_from: q = q.filter(Transaction.recon_date >= date_from)
    if date_to:   q = q.filter(Transaction.recon_date <= date_to)
    if partner:   q = q.filter(Transaction.partner == partner)

    rows = q.order_by(Transaction.match_id).limit(1000).all()
    # Group by match_id to show as pairs
    from collections import defaultdict
    pairs = defaultdict(list)
    for r in rows:
        pairs[r.match_id].append({
            "id": r.id, "partner": r.partner, "side": r.side,
            "recon_date": r.recon_date, "utr_number": r.utr_number,
            "amount": r.amount, "dr_cr": r.dr_cr,
        })

    return {"matches": [{"match_id": mid, "transactions": txns}
                        for mid, txns in pairs.items()],
            "count": len(pairs)}


@router.post("/manual-interbank-match")
def manual_interbank_match(
    req: ManualInterbankRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("override")),
):
    """
    Manually pair two fund_transfer transactions from any two bank accounts
    as an interbank match (IBT- prefix). Both must be bank-side fund_transfer rows.
    Remark is MANDATORY — rejected server-side if empty.
    Full human audit trail recorded.
    """
    _require_remark(req.remark, "manual_interbank_match")

    txn_a = db.query(Transaction).filter(Transaction.id == req.txn_id_a).first()
    txn_b = db.query(Transaction).filter(Transaction.id == req.txn_id_b).first()

    if not txn_a or not txn_b:
        raise HTTPException(status_code=404, detail="One or both transactions not found")
    if txn_a.id == txn_b.id:
        raise HTTPException(status_code=400, detail="Cannot match a transaction with itself")
    # Allow same partner only if sides differ (e.g., bank CR ↔ internal DR from same bank)
    if txn_a.partner == txn_b.partner and txn_a.side == txn_b.side and txn_a.recon_date == txn_b.recon_date:
        raise HTTPException(
            status_code=400,
            detail="Both transactions are from the same partner, same side, and same date. "
                   "Interbank match expects different banks or different sides."
        )

    # Eligible: fund_transfer or unmatched, either side (bank or internal)
    _eligible = {str(ReconStatus.fund_transfer), str(ReconStatus.unmatched)}
    if str(txn_a.recon_status) not in _eligible:
        raise HTTPException(status_code=400,
            detail=f"Transaction A status '{txn_a.recon_status}' is not eligible. Must be fund_transfer or unmatched.")
    if str(txn_b.recon_status) not in _eligible:
        raise HTTPException(status_code=400,
            detail=f"Transaction B status '{txn_b.recon_status}' is not eligible. Must be fund_transfer or unmatched.")

    from core.matching_engine import _next_seq
    partner = "interbank"
    d = txn_a.recon_date or ""
    seq = _next_seq(partner, d, db)
    mid = f"IBT-{d.replace('-','') if d else 'MANUAL'}-{seq:05d}"

    import datetime
    for txn, other_id in [(txn_a, txn_b.id), (txn_b, txn_a.id)]:
        prev = txn.recon_status
        txn.recon_status    = ReconStatus.interbank_matched
        txn.match_id        = mid
        txn.matched_with_id = other_id
        txn.override_note   = req.remark
        txn.override_by     = current_user.username
        txn.override_at     = datetime.datetime.utcnow()
        txn.prev_recon_status = str(prev)
        _human_log(db, current_user, "manual_interbank_match", txn.id,
                   str(prev), "interbank_matched", req.remark,
                   {"match_id": mid, "paired_with": other_id})

    db.commit()
    return {
        "message": "Manual interbank match created",
        "match_id": mid,
        "txn_a": req.txn_id_a,
        "txn_b": req.txn_id_b,
    }


@router.post("/manual-match-bulk")
def do_manual_match_bulk(
    req: BulkManualMatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manual_match")),
):
    """
    Match multiple bank↔internal pairs in a single request.
    Each pair is matched independently; errors on individual pairs are reported
    without stopping the rest.
    """
    if not req.pairs:
        raise HTTPException(status_code=400, detail="No pairs provided")

    results = []
    for pair in req.pairs:
        bank_id = pair.get("bank_txn_id")
        int_id  = pair.get("internal_txn_id")
        if not bank_id or not int_id:
            results.append({"bank_txn_id": bank_id, "internal_txn_id": int_id, "status": "error", "detail": "Missing ID"})
            continue
        try:
            result = manual_match(bank_id, int_id, db)
            _log(db, current_user, "manual_match", "transaction", bank_id,
                 {"matched_with": int_id, "match_id": result.get("match_id"),
                  "amount_mismatch": result.get("amount_mismatch"), "amount_diff": result.get("amount_diff")})
            results.append({"bank_txn_id": bank_id, "internal_txn_id": int_id, "status": "ok",
                            "match_id": result.get("match_id"),
                            "amount_mismatch": result.get("amount_mismatch", False),
                            "amount_diff": result.get("amount_diff", 0),
                            "recon_status": result.get("status")})
        except Exception as e:
            results.append({"bank_txn_id": bank_id, "internal_txn_id": int_id,
                            "status": "error", "detail": str(e)})

    ok_count       = sum(1 for r in results if r["status"] == "ok")
    err_count      = sum(1 for r in results if r["status"] == "error")
    mismatch_count = sum(1 for r in results if r.get("amount_mismatch"))
    return {"matched": ok_count, "errors": err_count, "mismatch": mismatch_count, "results": results}


@router.post("/assign-src")
def do_assign_src(req: SRCRequest, db: Session = Depends(get_db),
                  current_user: User = Depends(require_permission("src_assign"))):
    txn = assign_src(req.transaction_id, req.src_code, req.src_note, db)
    _log(db, current_user, "assign_src", "transaction", txn.id, {
        "src_code": req.src_code, "src_note": req.src_note
    })
    return {"message": "SRC assigned", "transaction_id": txn.id, "src_code": txn.src_code}

@router.post("/assign-src-bulk")
def do_assign_src_bulk(req: BulkSRCRequest, db: Session = Depends(get_db),
                       current_user: User = Depends(require_permission("src_assign"))):
    """
    Assign the same SRC code + note to a list of transaction IDs in one shot.
    Only updates rows that are currently unmatched or amount_mismatch.
    Returns count of rows actually updated.
    """
    if not req.transaction_ids:
        raise HTTPException(status_code=400, detail="No transaction IDs provided")
    if req.src_code not in SRC_CODES:
        raise HTTPException(status_code=400, detail=f"Invalid SRC code: {req.src_code}")

    eligible_statuses = [ReconStatus.unmatched, ReconStatus.amount_mismatch]
    txns = db.query(Transaction).filter(
        Transaction.id.in_(req.transaction_ids),
        Transaction.recon_status.in_(eligible_statuses)
    ).all()

    for txn in txns:
        txn.src_code    = req.src_code
        txn.src_note    = req.src_note
        txn.recon_status = ReconStatus.src_assigned

    db.commit()
    _log(db, current_user, "bulk_src", "transaction", detail={
        "src_code": req.src_code, "count": len(txns),
        "transaction_ids": req.transaction_ids[:20]   # cap log size
    })
    return {"updated": len(txns), "src_code": req.src_code}

@router.post("/manual-match")
def do_manual_match(req: ManualMatchRequest, db: Session = Depends(get_db),
                    current_user: User = Depends(require_permission("manual_match"))):
    queued = maker_checker.intercept(
        db, current_user, "manual_match",
        payload={"bank_txn_id": req.bank_txn_id, "internal_txn_id": req.internal_txn_id},
        summary=f"Manual match {req.bank_txn_id} ↔ {req.internal_txn_id}",
    )
    if queued:
        return queued
    result = manual_match(req.bank_txn_id, req.internal_txn_id, db)
    _log(db, current_user, "manual_match", "transaction", req.bank_txn_id, result)
    return result


class EditIdentifiersRequest(BaseModel):
    # None = leave unchanged; "" = clear the field. TID=eko_tid, Tracking/RRN=tracking_number, UTR=utr_number.
    eko_tid: Optional[str] = None
    tracking_number: Optional[str] = None
    utr_number: Optional[str] = None
    remark: str = ""


# Statuses that mean the row is part of a live match — its identifiers must not change
# under it (identifiers are matching keys + audit references). Break the match first.
_ID_LOCKED_STATUSES = {
    ReconStatus.matched, ReconStatus.manual_matched,
    ReconStatus.interbank_matched, ReconStatus.amount_mismatch,
}


@router.patch("/transaction/{txn_id}/identifiers")
def edit_transaction_identifiers(
    txn_id: str,
    body: EditIdentifiersRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("override")),
):
    """
    Correct a transaction's identifiers — TID (eko_tid), Tracking/RRN (tracking_number),
    UTR (utr_number). Override-gated. BLOCKED on rows that are part of a match (matched /
    manual_matched / interbank_matched / amount_mismatch, or any row carrying a match_id) —
    un-match first so a live pair's keys can never silently change under it. Every change
    is audited old→new with a mandatory remark.
    """
    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if txn.match_id or txn.recon_status in _ID_LOCKED_STATUSES:
        st = txn.recon_status.value if hasattr(txn.recon_status, "value") else str(txn.recon_status)
        raise HTTPException(
            status_code=400,
            detail=f"This entry is part of a match ({st}{', ' + txn.match_id if txn.match_id else ''}). "
                   f"Un-match it first, then edit its identifiers.")
    _require_remark(body.remark, "edit_identifiers")

    changes = {}
    for field, newval in (("eko_tid", body.eko_tid),
                          ("tracking_number", body.tracking_number),
                          ("utr_number", body.utr_number)):
        if newval is None:
            continue
        nv = newval.strip() or None
        oldv = getattr(txn, field)
        if nv != oldv:
            changes[field] = {"old": oldv, "new": nv}
            setattr(txn, field, nv)
    if not changes:
        raise HTTPException(status_code=400, detail="No identifier changes provided")

    prev = str(txn.recon_status)
    db.commit()
    _human_log(db, current_user, "edit_identifiers", txn.id, prev, prev, body.remark,
               {"changes": changes})
    return {"updated": txn.id, "changes": changes}

# Products whose data lives in their own tables (not the core transactions ledger).
# Open Items can still surface their unmatched / exception rows via this adapter.
_MODULE_OPEN = {
    "bbps":   ["unmatched_bank", "unmatched_internal", "failed_pending_refund",
               "refunded_but_success", "amount_mismatch", "src_assigned"],
    "evalue": ["unmatched_bank", "unmatched_load", "twice_credit", "wrong_amount", "src_assigned"],
}

# Product-level selections that fan out to several core-ledger partners. Lets the
# DMT recon window offer an "All Banks" consolidated view.
_PRODUCT_PARTNERS = {
    "dmt": ["fino", "airtel", "axis", "levin"],
}

# Universal Open-Items status buckets → the concrete module statuses they map to.
# Lets the shared "Matched / Unmatched / Amount Mismatch / Duplicate" filters work
# across BBPS + E-Value (whose rows use side-specific status strings), so every
# filter behaves the same at Partner = All as it does for the core ledger.
_UNIVERSAL_STATUS = {
    "matched_all":     ["matched", "manual_matched"],
    "unmatched":       ["unmatched_bank", "unmatched_internal", "unmatched_load"],
    "amount_mismatch": ["amount_mismatch", "wrong_amount"],
    "duplicate":       ["duplicate"],
}


def _module_rows(module, *, recon_date=None, date_from=None, date_to=None, side=None,
                 recon_status=None, eko_tid=None, tracking_number=None, match_id=None,
                 amount_min=None, amount_max=None, aging=None, bank_description=None,
                 csp_code=None, csp_name=None, db=None):
    """Full filtered list of module rows in the core open-items shape (no pagination).

    Honours every Open-Items filter so combinations behave identically to the core
    ledger. Default = open/exception rows; a specific status or a Recon-ID lookup
    overrides that to return matched rows too.
    """
    import datetime as _dt
    open_set = _MODULE_OPEN.get(module, [])

    def _age(d):
        try:
            return (_dt.date.today() - _dt.date.fromisoformat((d or "")[:10])).days
        except Exception:
            return None

    rows = []
    mid_q = (match_id or "").strip().lower()
    tid_list = [t.strip().lower() for t in (eko_tid or "").strip('()[]{} ').split(',') if t.strip()]
    trk_list = [t.strip().lower() for t in (tracking_number or "").strip('()[]{} ').split(',') if t.strip()]
    desc_q = (bank_description or "").strip().lower()
    csp_code_q = (csp_code or "").strip().lower()
    csp_name_q = (csp_name or "").strip().lower()

    # Resolve which concrete module statuses this filter targets (None = no status gate).
    #   match_id lookup → ignore status   |   "all" → everything
    #   universal bucket → its mapped set |   specific status → itself
    #   default ("")     → this module's open/exception set
    if mid_q or recon_status == "all":
        _allowed = None
    elif recon_status:
        _allowed = set(_UNIVERSAL_STATUS.get(recon_status, [recon_status]))
    else:
        _allowed = set(open_set)

    def _collect(query, datecol, mapper, want_side):
        if side and side != want_side:
            return
        for r in query.all():
            st = r.recon_status
            if mid_q:
                if mid_q not in str(getattr(r, "match_id", "") or "").lower():
                    continue
            elif _allowed is not None and st not in _allowed:
                continue
            d = getattr(r, datecol)
            if recon_date and d != recon_date:      continue
            if date_from and (d or "") < date_from: continue
            if date_to and (d or "") > date_to:     continue
            item = mapper(r)
            if tid_list and not any(t in str(item.get("eko_tid") or "").lower() for t in tid_list):
                continue
            if trk_list and not any(t in str(item.get("tracking_number") or "").lower() for t in trk_list):
                continue
            if desc_q and desc_q not in str(item.get("bank_description") or "").lower():
                continue
            if csp_code_q and csp_code_q not in str(item.get("csp_code") or "").lower():
                continue
            if csp_name_q and csp_name_q not in str(item.get("csp_name") or "").lower():
                continue
            try:
                amt = float(item.get("amount") or 0)
                if amount_min is not None and amt < float(amount_min): continue
                if amount_max is not None and amt > float(amount_max): continue
            except (TypeError, ValueError):
                pass
            days = _age(d)
            if aging:
                if days is None: continue
                if aging == "d1" and not (1 <= days <= 2):  continue
                if aging == "d3" and not (3 <= days <= 6):  continue
                if aging == "d7" and not (days >= 7):       continue
            item["days_open"] = days
            rows.append(item)

    if module == "bbps":
        from models.database import BbpsBankTxn, BbpsInternal
        _collect(db.query(BbpsBankTxn), "transaction_date", lambda b: {
            "id": b.id, "partner": "bbps", "side": "bank", "recon_date": b.transaction_date,
            "transaction_date": b.transaction_date, "eko_tid": b.client_ref,
            "tracking_number": b.operator_ref, "utr_number": None, "amount": b.amount,
            "dr_cr": None, "status": b.status, "recon_status": b.recon_status,
            "match_id": b.match_id, "src_code": b.src_code, "src_note": b.src_note,
            "assigned_to": None, "exception_reason": None, "row_type": "txn",
            "bank_description": None,  # BBPS operator reports carry no free-text narration
            "csp_code": None, "csp_name": None,  # CSP is on the internal side only
        }, "bank")
        _collect(db.query(BbpsInternal), "transaction_date", lambda i: {
            "id": i.id, "partner": "bbps", "side": "internal", "recon_date": i.transaction_date,
            "transaction_date": i.transaction_date, "eko_tid": i.eko_trxn_id,
            "tracking_number": i.tracking_number, "utr_number": None, "amount": i.amount,
            "dr_cr": None, "status": i.status, "recon_status": i.recon_status,
            "match_id": i.match_id, "src_code": i.src_code, "src_note": i.src_note,
            "assigned_to": None, "exception_reason": None, "row_type": "txn",
            "bank_description": None,
            "csp_code": i.csp_code, "csp_name": i.merchant_name,  # CSP already stored
        }, "internal")
    elif module == "evalue":
        from models.database import EvalueBankTxn, EvalueWalletLoad
        _collect(db.query(EvalueBankTxn), "txn_date", lambda b: {
            "id": b.id, "partner": "evalue", "side": "bank", "recon_date": b.txn_date,
            "transaction_date": b.txn_date, "eko_tid": b.utr or b.atm_ref,
            "tracking_number": b.reco_acc_no, "utr_number": b.utr, "amount": b.amount,
            "dr_cr": b.dr_cr, "status": None, "recon_status": b.recon_status,
            "match_id": b.match_id, "src_code": b.src_code, "src_note": b.src_note,
            "assigned_to": None, "exception_reason": None, "row_type": "txn",
            "bank_description": b.description,  # E-Value bank txns already store the narration
            "csp_code": None, "csp_name": None,  # CSP is on the internal side only
        }, "bank")
        _collect(db.query(EvalueWalletLoad), "transaction_date", lambda l: {
            "id": l.id, "partner": "evalue", "side": "internal", "recon_date": l.transaction_date,
            "transaction_date": l.transaction_date, "eko_tid": l.eko_trxn_id,
            "tracking_number": l.reco_acc_no, "utr_number": l.utr_number, "amount": l.amount,
            "dr_cr": None, "status": l.status, "recon_status": l.recon_status,
            "match_id": l.match_id, "src_code": l.src_code, "src_note": l.src_note,
            "assigned_to": None, "exception_reason": None, "row_type": "txn",
            "bank_description": None,
            "csp_code": l.csp_code, "csp_name": l.merchant_name,  # CSP already stored
        }, "internal")

    rows.sort(key=lambda x: str(x.get("recon_date") or ""), reverse=True)
    return rows


def _all_module_rows(db, **filters):
    """Concatenate every open-items-capable module's rows (BBPS + E-Value), sorted."""
    out = []
    for _m in _MODULE_OPEN:
        out.extend(_module_rows(_m, db=db, **filters))
    out.sort(key=lambda x: str(x.get("recon_date") or ""), reverse=True)
    return out


def _module_open_items(module, db, page, page_size, **filters):
    rows = _module_rows(module, db=db, **filters)
    total = len(rows)
    start = (page - 1) * page_size
    return {"total": total, "page": page, "page_size": page_size, "items": rows[start:start + page_size]}


@router.get("/open-items")
def get_open_items(
    partner: Optional[str] = None,
    recon_date: Optional[str] = None,   # single date (legacy / dashboard click-through)
    date_from: Optional[str] = None,    # range start
    date_to: Optional[str] = None,      # range end
    side: Optional[str] = None,
    src_code: Optional[str] = None,
    eko_tid: Optional[str] = None,          # single or comma-sep list
    tracking_number: Optional[str] = None,  # single or comma-sep list
    match_id: Optional[str] = None,
    recon_status: Optional[str] = None,
    aging: Optional[str] = None,         # "d1" | "d3" | "d7"
    bank_account: Optional[str] = None,  # filter bank rows by source account number
    bank_description: Optional[str] = None,  # substring search over bank narration
    csp_code: Optional[str] = None,      # substring search over CSP code (internal rows)
    csp_name: Optional[str] = None,      # substring search over CSP name (internal rows)
    assigned: Optional[str] = None,      # "me" | "unassigned" | "<username>"
    row_type: Optional[str] = None,
    amount_min:       Optional[float] = None,
    amount_max:       Optional[float] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Module products (BBPS, E-Value) live in their own tables — serve them
    # through the adapter so Open Items can pull every product from one window.
    _mod_filters = dict(recon_date=recon_date, date_from=date_from, date_to=date_to,
                        side=side, recon_status=recon_status, eko_tid=eko_tid,
                        tracking_number=tracking_number, match_id=match_id,
                        amount_min=amount_min, amount_max=amount_max, aging=aging,
                        bank_description=bank_description,
                        csp_code=csp_code, csp_name=csp_name)
    if partner in _MODULE_OPEN:
        return _module_open_items(partner, db, page, page_size, **_mod_filters)

    q = db.query(Transaction)

    # row_type filter — skip the txn restriction for status values that span row types
    _skip_row_type = ("fee_matched", "fund_transfer", "interbank_matched", "all")
    if row_type:
        q = q.filter(Transaction.row_type == row_type)
    elif recon_status in _skip_row_type or match_id:
        pass   # a Recon-ID lookup may span row types — don't restrict
    else:
        q = q.filter(Transaction.row_type == "txn")

    # Status filter
    if recon_status == "all":
        pass
    elif recon_status == "matched_all":
        # Matched (DMT) — all auto + manual matches for standard txn recon
        q = q.filter(Transaction.recon_status.in_([
            ReconStatus.matched, ReconStatus.manual_matched,
        ]))
    elif recon_status in (
        "matched", "manual_matched", "unmatched", "src_assigned",
        "fee_matched", "fund_transfer", "amount_mismatch", "duplicate",
        "failed", "interbank_matched", "internal_matched", "adhoc_settlement",
        "reversal_matched", "human_override",
    ):
        q = q.filter(Transaction.recon_status == recon_status)
    elif match_id:
        pass   # Recon-ID lookup → return the whole match regardless of status
    else:
        # Default "Open Items" — only genuinely unresolved entries
        q = q.filter(Transaction.recon_status.in_([
            ReconStatus.unmatched,
            ReconStatus.src_assigned,
        ]))

    # Product-level partner (e.g. "dmt") fans out to its constituent banks so the
    # DMT recon window's "All Banks" option shows a consolidated view.
    if partner:
        _slugs = _PRODUCT_PARTNERS.get(partner)
        q = q.filter(Transaction.partner.in_(_slugs)) if _slugs else q.filter(Transaction.partner == partner)
    if side:    q = q.filter(Transaction.side == side)
    if src_code: q = q.filter(Transaction.src_code == src_code)
    if bank_account: q = q.filter(Transaction.bank_account == bank_account)
    if match_id: q = q.filter(Transaction.match_id.ilike(f"%{match_id}%"))
    if bank_description:
        q = q.filter(Transaction.bank_description.ilike(f"%{bank_description}%"))
    if csp_code:
        q = q.filter(Transaction.csp_code.ilike(f"%{csp_code}%"))
    if csp_name:
        q = q.filter(Transaction.csp_name.ilike(f"%{csp_name}%"))

    # Exception-workflow ownership filter
    if assigned == "me":
        q = q.filter(Transaction.assigned_to == current_user.username)
    elif assigned == "unassigned":
        q = q.filter(Transaction.assigned_to.is_(None))
    elif assigned:
        q = q.filter(Transaction.assigned_to == assigned)

    # Date filter: single date takes precedence, then range
    if recon_date:
        q = q.filter(Transaction.recon_date == recon_date)
    else:
        if date_from: q = q.filter(Transaction.recon_date >= date_from)
        if date_to:   q = q.filter(Transaction.recon_date <= date_to)

    # Multi-value TID filter: comma-sep → exact IN, single → LIKE
    # Strip surrounding parentheses/brackets that users may paste: (A,B,C) → A,B,C
    if eko_tid:
        clean = eko_tid.strip().strip('()[]{} ')
        tids = [t.strip() for t in clean.split(',') if t.strip()]
        if len(tids) > 1:
            q = q.filter(Transaction.eko_tid.in_(tids))
        elif tids:
            q = q.filter(Transaction.eko_tid.ilike(f"%{tids[0]}%"))

    # Multi-value Tracking filter
    if tracking_number:
        clean_t = tracking_number.strip().strip('()[]{} ')
        tracks = [t.strip() for t in clean_t.split(',') if t.strip()]
        if len(tracks) > 1:
            q = q.filter(Transaction.tracking_number.in_(tracks))
        elif tracks:
            q = q.filter(Transaction.tracking_number.ilike(f"%{tracks[0]}%"))

    # Amount range filter
    if amount_min is not None: q = q.filter(Transaction.amount >= amount_min)
    if amount_max is not None: q = q.filter(Transaction.amount <= amount_max)

    # Aging filter
    if aging:
        today = datetime.date.today().isoformat()
        if aging == "d1":
            q = q.filter(Transaction.recon_date < today,
                         Transaction.recon_date >= (datetime.date.today() - datetime.timedelta(days=2)).isoformat())
        elif aging == "d3":
            q = q.filter(Transaction.recon_date < (datetime.date.today() - datetime.timedelta(days=1)).isoformat(),
                         Transaction.recon_date >= (datetime.date.today() - datetime.timedelta(days=4)).isoformat())
        elif aging == "d7":
            q = q.filter(Transaction.recon_date < (datetime.date.today() - datetime.timedelta(days=4)).isoformat())

    core_total = q.count()
    ordered = q.order_by(Transaction.recon_date.desc(), Transaction.transaction_date.desc())

    # Partner = All → union the core ledger with the module products (BBPS, E-Value)
    # so every product is visible from one window. Core rows come first, then modules.
    if not partner:
        module_items = _all_module_rows(db, **_mod_filters)
        module_total = len(module_items)
        offset = (page - 1) * page_size
        core_part = ordered.offset(offset).limit(page_size).all() if offset < core_total else []
        need = page_size - len(core_part)
        mod_start = max(0, offset - core_total)
        module_part = module_items[mod_start:mod_start + need] if need > 0 else []
        return {
            "total": core_total + module_total,
            "page": page,
            "page_size": page_size,
            "items": [_txn_to_dict(t) for t in core_part] + module_part,
        }

    items = ordered.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": core_total,
        "page": page,
        "page_size": page_size,
        "items": [_txn_to_dict(t) for t in items]
    }

@router.get("/matched-items")
def get_matched_items(
    partner: Optional[str] = None,
    recon_date: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    q = db.query(Transaction).filter(
        Transaction.recon_status.in_([ReconStatus.matched, ReconStatus.manual_matched]),
        Transaction.side == "bank"
    )
    if partner:    q = q.filter(Transaction.partner == partner)
    if recon_date: q = q.filter(Transaction.recon_date == recon_date)
    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [_txn_to_dict(t) for t in items]}

@router.get("/dashboard-summary")
def dashboard_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # ── Resolve active partners dynamically ───────────────────────────────────
    # 1. All partners that actually have transactions in the DB
    txn_partner_q = db.query(Transaction.partner).filter(Transaction.partner.isnot(None))
    if date_from: txn_partner_q = txn_partner_q.filter(Transaction.recon_date >= date_from)
    if date_to:   txn_partner_q = txn_partner_q.filter(Transaction.recon_date <= date_to)
    db_partners = {p[0] for p in txn_partner_q.distinct().all() if p[0]}

    # 2. Active partners from PartnerConfig (so empty-data partners still show in UI)
    try:
        from models.database import PartnerConfig
        cfg_partners = {p.slug for p in db.query(PartnerConfig).filter(PartnerConfig.is_active == True).all()}
        db_partners.update(cfg_partners)
    except Exception as _e:
        logger.warning(f"dashboard_summary: could not load PartnerConfig: {_e}")
    partners = sorted(db_partners) if db_partners else ["fino", "airtel"]
    sides    = ["internal", "bank"]

    result = {"internal": {}, "bank": {}}

    # ── Single GROUP BY query replaces ~10 COUNT queries per partner×date ────────
    # Aggregates count and amount by (partner, side, recon_date, row_type, recon_status)
    from sqlalchemy import func as sqlfunc, case

    base_q = db.query(
        Transaction.partner,
        Transaction.side,
        Transaction.recon_date,
        Transaction.row_type,
        Transaction.recon_status,
        sqlfunc.count(Transaction.id).label("cnt"),
        sqlfunc.sum(Transaction.amount).label("amt"),
    ).filter(
        Transaction.partner.in_(partners),
        Transaction.recon_date.isnot(None),
    )
    if date_from: base_q = base_q.filter(Transaction.recon_date >= date_from)
    if date_to:   base_q = base_q.filter(Transaction.recon_date <= date_to)
    agg_rows = base_q.group_by(
        Transaction.partner, Transaction.side,
        Transaction.recon_date, Transaction.row_type, Transaction.recon_status
    ).all()

    # Build lookup: (partner, side, date) → {stat_key: value}
    agg = {}
    for row in agg_rows:
        key = (row.partner, row.side, row.recon_date)
        if key not in agg:
            agg[key] = {"total": 0, "txn_total": 0, "matched": 0, "unmatched": 0,
                        "src_assigned": 0, "amount_mismatch": 0, "duplicate": 0,
                        "failed": 0, "fee_charge": 0, "fee_matched": 0,
                        "settlement_credit": 0, "fund_transfer": 0,
                        "amount_matched": 0.0, "amount_open": 0.0}
        d = agg[key]
        cnt = row.cnt or 0
        amt = float(row.amt or 0)
        d["total"] += cnt
        if row.row_type == "txn":
            d["txn_total"] += cnt
            rs = row.recon_status
            if rs in (ReconStatus.matched, ReconStatus.manual_matched):
                d["matched"] += cnt; d["amount_matched"] += amt
            elif rs == ReconStatus.unmatched:
                d["unmatched"] += cnt; d["amount_open"] += amt
            elif rs == ReconStatus.src_assigned:  d["src_assigned"] += cnt
            elif rs == ReconStatus.amount_mismatch: d["amount_mismatch"] += cnt
            elif rs == ReconStatus.duplicate:     d["duplicate"] += cnt
            elif rs == ReconStatus.failed:        d["failed"] += cnt
        elif row.row_type == "fee_charge":       d["fee_charge"] += cnt
        elif row.row_type == "settlement_credit": d["settlement_credit"] += cnt
        if row.recon_status == ReconStatus.fee_matched:  d["fee_matched"] += cnt
        if row.recon_status == ReconStatus.fund_transfer: d["fund_transfer"] += cnt

    today_date = datetime.date.today()

    for side in sides:
        for partner in partners:
            # Collect dates for this partner+side from agg keys
            dates = sorted(
                {k[2] for k in agg if k[0] == partner and k[1] == side},
                reverse=True
            )
            rows = []
            for date in dates:
                d = agg.get((partner, side, date), {})
                txn_total = d.get("txn_total", 0)
                matched   = d.get("matched", 0)
                try:
                    row_date = datetime.date.fromisoformat(date)
                    days_open = (today_date - row_date).days
                    aging_bucket = ("current" if days_open == 0 else
                                    "d1" if days_open == 1 else
                                    "d3" if days_open <= 3 else "d7+")
                except Exception:
                    days_open, aging_bucket = None, None

                rows.append({
                    "date":             date,
                    "total":            d.get("total", 0),
                    "txn_total":        txn_total,
                    "matched":          matched,
                    "unmatched":        d.get("unmatched", 0),
                    "src_assigned":     d.get("src_assigned", 0),
                    "amount_mismatch":  d.get("amount_mismatch", 0),
                    "duplicate":        d.get("duplicate", 0),
                    "failed":           d.get("failed", 0),
                    "fee_charge":       d.get("fee_charge", 0),
                    "fee_matched":      d.get("fee_matched", 0),
                    "settlement_credit":d.get("settlement_credit", 0),
                    "fund_transfer":    d.get("fund_transfer", 0),
                    "match_rate":       round(matched / txn_total * 100, 1) if txn_total > 0 else 0,
                    "days_open":        days_open,
                    "aging_bucket":     aging_bucket,
                    "amount_matched":   round(d.get("amount_matched", 0), 2),
                    "amount_open":      round(d.get("amount_open", 0), 2),
                })
            result[side][partner] = rows

    return result

@router.get("/distinct-dates")
def distinct_dates(
    partner: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Return distinct recon_dates that have transactions (for run-range UI)."""
    q = db.query(Transaction.recon_date).filter(Transaction.recon_date.isnot(None))
    if partner:   q = q.filter(Transaction.partner == partner)
    if date_from: q = q.filter(Transaction.recon_date >= date_from)
    if date_to:   q = q.filter(Transaction.recon_date <= date_to)
    dates = q.distinct().order_by(Transaction.recon_date).all()
    return [d[0] for d in dates if d[0]]

@router.get("/runs")
def list_runs(partner: Optional[str] = None, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    q = db.query(ReconRun)
    if partner: q = q.filter(ReconRun.partner == partner)
    runs = q.order_by(ReconRun.run_at.desc()).limit(50).all()
    return [{"id": r.id, "partner": r.partner, "recon_date": r.recon_date,
             "run_at": str(r.run_at), "matched": r.matched,
             "unmatched_bank": r.unmatched_bank, "unmatched_internal": r.unmatched_internal,
             "total_bank": r.total_bank, "total_internal": r.total_internal,
             "rules_applied": json.loads(r.rules_applied or "[]")} for r in runs]

@router.get("/src-codes")
def get_src_codes():
    return SRC_CODES


# ─── Mismatch Drill-down ──────────────────────────────────────────────────────

@router.get("/mismatches")
def get_mismatches(
    partner: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Return all amount_mismatch paired transactions with delta.
    Each row = bank TID, bank amount, internal TID, internal amount, delta, match_id.
    """
    q = db.query(Transaction).filter(
        Transaction.recon_status == ReconStatus.amount_mismatch,
        Transaction.side == "bank"
    )
    if partner:   q = q.filter(Transaction.partner == partner)
    if date_from: q = q.filter(Transaction.recon_date >= date_from)
    if date_to:   q = q.filter(Transaction.recon_date <= date_to)
    bank_txns = q.all()

    # Fetch internal counterparts in one query
    internal_ids = [t.matched_with_id for t in bank_txns if t.matched_with_id]
    internals = {}
    if internal_ids:
        internals = {t.id: t for t in db.query(Transaction).filter(Transaction.id.in_(internal_ids)).all()}

    result = []
    for b in bank_txns:
        i = internals.get(b.matched_with_id)
        bank_amt = b.amount or 0
        int_amt  = (i.amount if i else 0) or 0
        result.append({
            "match_id":          b.match_id,
            "partner":           b.partner,
            "recon_date":        b.recon_date,
            "bank_txn_id":       b.id,
            "bank_eko_tid":      b.eko_tid or "—",
            "bank_tracking":     b.tracking_number or "—",
            "bank_amount":       round(bank_amt, 2),
            "bank_description":  getattr(b, "bank_description", None),
            "internal_txn_id":   i.id if i else None,
            "internal_eko_tid":  i.eko_tid if i else "—",
            "internal_amount":   round(int_amt, 2),
            # CSP (retailer) identity of the internal counterpart (read-only).
            "internal_csp_code": (getattr(i, "csp_code", None) if i else None),
            "internal_csp_name": (getattr(i, "csp_name", None) if i else None),
            "delta":             round(bank_amt - int_amt, 2),
            "core":              True,
            "product":           (b.partner or "").upper(),
        })

    # ── Product mismatches (E-Value wrong_amount, BBPS amount_mismatch) — shown here so
    # the Mismatches page is cross-product, not core-ledger only. READ-ONLY here: each is
    # resolved in its own product window (the resolve endpoint below stays core-only).
    # Wrapped so a schema surprise can never break the core mismatches view.
    try:
        from models.database import EvalueBankTxn, EvalueWalletLoad, BbpsBankTxn, BbpsInternal
        _evb = db.query(EvalueBankTxn).filter(EvalueBankTxn.recon_status == "wrong_amount")
        if date_from: _evb = _evb.filter(EvalueBankTxn.txn_date >= date_from)
        if date_to:   _evb = _evb.filter(EvalueBankTxn.txn_date <= date_to)
        _evb = _evb.all()
        _evm = [b.match_id for b in _evb if b.match_id]
        _evl = {l.match_id: l for l in db.query(EvalueWalletLoad).filter(EvalueWalletLoad.match_id.in_(_evm)).all()} if _evm else {}
        for b in _evb:
            l = _evl.get(b.match_id); ba = float(b.amount or 0); la = float((l.amount if l else 0) or 0)
            result.append({"match_id": b.match_id, "partner": "evalue", "product": "E-Value",
                "core": False, "resolve_in": "/evalue", "recon_date": b.txn_date, "bank_txn_id": b.id,
                "bank_eko_tid": b.utr or "—", "bank_tracking": b.ref_no or "—", "bank_amount": round(ba, 2),
                "bank_description": b.description, "internal_txn_id": (l.id if l else None),
                "internal_eko_tid": (l.eko_trxn_id if l else "—"), "internal_amount": round(la, 2),
                "internal_csp_code": (l.csp_code if l else None), "internal_csp_name": (l.merchant_name if l else None),
                "delta": round(ba - la, 2)})
        _bbb = db.query(BbpsBankTxn).filter(BbpsBankTxn.recon_status == "amount_mismatch")
        if date_from: _bbb = _bbb.filter(BbpsBankTxn.transaction_date >= date_from)
        if date_to:   _bbb = _bbb.filter(BbpsBankTxn.transaction_date <= date_to)
        _bbb = _bbb.all()
        _bbm = [b.match_id for b in _bbb if b.match_id]
        _bbi = {i.match_id: i for i in db.query(BbpsInternal).filter(BbpsInternal.match_id.in_(_bbm)).all()} if _bbm else {}
        for b in _bbb:
            i = _bbi.get(b.match_id); ba = float(b.amount or 0); ia = float((i.amount if i else 0) or 0)
            result.append({"match_id": b.match_id, "partner": "bbps", "product": "BBPS",
                "core": False, "resolve_in": "/bbps", "recon_date": b.transaction_date, "bank_txn_id": b.id,
                "bank_eko_tid": (b.provider or "—"), "bank_tracking": "—", "bank_amount": round(ba, 2),
                "bank_description": None, "internal_txn_id": (i.id if i else None),
                "internal_eko_tid": (i.eko_trxn_id if i else "—"), "internal_amount": round(ia, 2),
                "internal_csp_code": (i.csp_code if i else None), "internal_csp_name": (i.merchant_name if i else None),
                "delta": round(ba - ia, 2)})
    except Exception:
        pass

    return sorted(result, key=lambda x: (x["recon_date"] or ""), reverse=True)


@router.post("/mismatches/{bank_txn_id}/resolve")
def resolve_mismatch(
    bank_txn_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("src_assign"))
):
    """
    Ops approves the amount discrepancy → override amount_mismatch → matched.
    Both sides of the pair are updated.
    """
    bank_txn = db.query(Transaction).filter(
        Transaction.id == bank_txn_id,
        Transaction.recon_status == ReconStatus.amount_mismatch
    ).first()
    if not bank_txn:
        raise HTTPException(status_code=404, detail="Mismatch transaction not found")
    internal_txn = db.query(Transaction).filter(
        Transaction.id == bank_txn.matched_with_id
    ).first() if bank_txn.matched_with_id else None

    bank_txn.recon_status = ReconStatus.matched
    if internal_txn:
        internal_txn.recon_status = ReconStatus.matched
    db.commit()

    _log(db, current_user, "resolve_mismatch", "transaction", bank_txn_id, {
        "match_id": bank_txn.match_id,
        "bank_amount": bank_txn.amount,
        "internal_amount": internal_txn.amount if internal_txn else None,
    })
    return {"message": "Resolved", "match_id": bank_txn.match_id}


# ─── Rule Management ──────────────────────────────────────────────────────────
@router.get("/rules")
def list_rules(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rules = db.query(MatchRule).order_by(MatchRule.partner, MatchRule.priority).all()
    return [{"id": r.id, "name": r.name, "partner": r.partner, "priority": r.priority,
             "match_fields": json.loads(r.match_fields), "is_active": r.is_active,
             "description": r.description} for r in rules]

@router.post("/rules")
def create_rule(data: RuleCreate, db: Session = Depends(get_db),
                current_user: User = Depends(require_permission("logic_builder"))):
    rule = MatchRule(
        name=data.name,
        partner=data.partner,
        priority=data.priority,
        match_fields=json.dumps(data.match_fields),
        description=data.description,
        created_by=current_user.id
    )
    db.add(rule)
    db.commit()
    return {"id": rule.id, "message": "Rule created"}

@router.put("/rules/{rule_id}")
def update_rule(rule_id: str, data: RuleCreate, db: Session = Depends(get_db),
                current_user: User = Depends(require_permission("logic_builder"))):
    """Edit a matching rule in place (Logic Builder). Same permission as create/toggle/
    delete, so the Logic Builder is a full editor — not create-and-delete only."""
    rule = db.query(MatchRule).filter(MatchRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    rule.name = data.name
    rule.partner = data.partner
    rule.priority = data.priority
    rule.match_fields = json.dumps(data.match_fields)
    rule.description = data.description
    db.commit()
    return {"id": rule.id, "message": "Rule updated"}

@router.put("/rules/{rule_id}/toggle")
def toggle_rule(rule_id: str, db: Session = Depends(get_db),
                current_user: User = Depends(require_permission("logic_builder"))):
    rule = db.query(MatchRule).filter(MatchRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    rule.is_active = not rule.is_active
    db.commit()
    return {"id": rule.id, "is_active": rule.is_active}

@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: str, db: Session = Depends(get_db),
                current_user: User = Depends(require_permission("logic_builder"))):
    rule = db.query(MatchRule).filter(MatchRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.delete(rule)
    db.commit()
    return {"message": "Deleted"}


# ─── PG Net Amount Settlement Summary ────────────────────────────────────────
@router.get("/pg-settlement-summary")
def pg_settlement_summary(
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Date-wise summary of PG (Accept Payment) transactions from the PayU bank side.
    Returns gross amount, net amount (post-fee), and total fees per recon date.
    Used by Dashboard PG panel and Reports page.
    """
    from sqlalchemy import func

    q = db.query(
        Transaction.recon_date,
        func.count(Transaction.id).label("txn_count"),
        func.sum(Transaction.amount).label("gross_total"),
        func.sum(Transaction.net_amount).label("net_total"),
    ).filter(
        Transaction.partner == "pg",
        Transaction.side == "bank",
        Transaction.row_type == "txn",
    )
    if date_from:
        q = q.filter(Transaction.recon_date >= date_from)
    if date_to:
        q = q.filter(Transaction.recon_date <= date_to)

    rows = q.group_by(Transaction.recon_date).order_by(Transaction.recon_date.desc()).all()

    result = []
    for r in rows:
        gross = round(r.gross_total or 0, 2)
        net   = round(r.net_total   or 0, 2)
        fees  = round(gross - net, 2)
        result.append({
            "date":        r.recon_date,
            "txn_count":   r.txn_count,
            "gross_total": gross,
            "net_total":   net,
            "fees_total":  fees,
        })

    return {
        "rows": result,
        "summary": {
            "txn_count":   sum(r["txn_count"]   for r in result),
            "gross_total": round(sum(r["gross_total"] for r in result), 2),
            "net_total":   round(sum(r["net_total"]   for r in result), 2),
            "fees_total":  round(sum(r["fees_total"]  for r in result), 2),
        }
    }


# ─── Shared serializer ────────────────────────────────────────────────────────
def _txn_to_dict(t: Transaction) -> dict:
    aging_bucket = None
    days_open    = None
    if t.recon_status in (ReconStatus.unmatched, ReconStatus.src_assigned, ReconStatus.amount_mismatch,
                          ReconStatus.duplicate, ReconStatus.failed):
        try:
            today     = datetime.date.today()
            row_date  = datetime.date.fromisoformat(t.recon_date)
            days_open = (today - row_date).days
            aging_bucket = (
                "current" if days_open == 0 else
                "D+1"     if days_open == 1 else
                "D+3"     if days_open <= 3 else
                "D+7+"
            )
        except Exception:
            pass

    return {
        "id": t.id, "partner": t.partner, "side": t.side,
        "recon_date": t.recon_date, "eko_tid": t.eko_tid,
        "tracking_number": t.tracking_number, "utr_number": t.utr_number,
        "amount": t.amount, "dr_cr": t.dr_cr,
        "status": t.status, "transaction_date": t.transaction_date,
        "recon_status": t.recon_status, "src_code": t.src_code, "src_note": t.src_note,
        "matched_with_id": t.matched_with_id,
        "match_id": getattr(t, "match_id", None),
        "row_type": getattr(t, "row_type", "txn"),
        "assigned_to": getattr(t, "assigned_to", None),
        "exception_reason": getattr(t, "exception_reason", None),
        "bank_account": getattr(t, "bank_account", None),
        # Read-only bank-statement narration (bank side only; NULL for internal).
        "bank_description": (getattr(t, "bank_description", None) if t.side == "bank" else None),
        # Running statement balance (bank side; display/funds-position only).
        "balance": (getattr(t, "balance", None) if t.side == "bank" else None),
        # Read-only CSP (retailer) identity (internal side only; NULL for bank).
        "csp_code": (getattr(t, "csp_code", None) if t.side == "internal" else None),
        "csp_name": (getattr(t, "csp_name", None) if t.side == "internal" else None),
        "days_open": days_open,
        "aging_bucket": aging_bucket,
    }
