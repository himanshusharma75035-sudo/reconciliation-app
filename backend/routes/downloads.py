"""
routes/downloads.py — Raw source-data download center.

Re-exports the INGESTED source rows (bank statements + internal dumps) exactly as stored,
per product + side, filtered by a business-date range — so operators can self-serve a copy
of any uploaded statement/dump at any time (e.g. to hand to another team).

This is a re-export from the parsed rows, NOT the original uploaded file byte-for-byte
(module products discard the raw file at ingest; only parsed rows are retained). The data is
faithful; the exact original layout is not reproduced.

Sensitive: these rows carry real account numbers / customer data, so the whole router is
gated by the dedicated `data_download` permission and every download is written to the audit
log (who pulled which product/side/date-range, when) — a gap the recon-report exports leave open.
"""
import io
import json
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, DateTime
from sqlalchemy.orm import Session

from models.database import (
    get_db, AuditLog, Transaction,
    EvalueBankTxn, EvalueWalletLoad, BbpsBankTxn, BbpsInternal,
    SBIBankTransaction, SBITxnReport,
)
from core.auth import require_permission

router = APIRouter(prefix="/api/downloads", tags=["downloads"])

# Products whose raw source rows live in their OWN tables (not the core Transaction ledger).
# key -> {label, bank: (Model, business_date_col), internal: (Model, business_date_col)}
_MODULE_SOURCES = {
    "evalue": {"label": "E-Value",
               "bank": (EvalueBankTxn, "txn_date"),
               "internal": (EvalueWalletLoad, "transaction_date")},
    "bbps":   {"label": "BBPS",
               "bank": (BbpsBankTxn, "transaction_date"),
               "internal": (BbpsInternal, "transaction_date")},
    "kiosk":  {"label": "SBI Kiosk",
               "bank": (SBIBankTransaction, "txn_date"),
               "internal": (SBITxnReport, "txn_date")},
}
_SIDE_LABEL = {"bank": "Bank Statement", "internal": "Internal Data"}
_CORE_DATE_COL = "recon_date"     # core ledger filters on the normalised business date


# Core-ledger internal plumbing NOT wanted in a clean statement export — the original uploaded
# columns are expanded from `raw_data` instead, so these are dropped.
_CORE_DROP = {"id", "upload_session_id", "raw_data", "partner", "side", "row_type", "net_amount",
              "matched_with_id", "prev_recon_status", "override_note", "override_by",
              "assigned_to", "exception_reason", "src_code", "src_note", "csp_code", "csp_name"}
# Module tables are typed columns already — just drop bookkeeping.
_MODULE_DROP = {"id", "created_at", "upload_date"}


def _status_label(s) -> str:
    return str(s or "").replace("_", " ").strip().title() or "—"


def _summ(q, date_expr):
    # count over ALL rows; date span over REAL dates only, so the core ledger's `recon_date`
    # sentinels ('auto' / 'auto (multi-date)') don't leak into the displayed range.
    cnt = q.with_entities(func.count()).scalar() or 0
    mn, mx = q.with_entities(func.min(date_expr), func.max(date_expr)).filter(date_expr.like("20%")).one()
    return {"count": int(cnt), "min_date": mn, "max_date": mx}


def _accounts_for(db, product, side):
    """Distinct bank accounts for a product+side, so multi-account products (e.g. Axis has 3
    accounts, E-Value spans ~24 banks) can be downloaded per account. Returns [] when there is
    only one / none — the UI then hides the account picker."""
    if product in _MODULE_SOURCES:
        if product == "evalue":
            model, _ = _MODULE_SOURCES[product][side]
            col = model.reco_acc_no
            vals = [a for (a,) in db.query(col).filter(col.isnot(None), col != "").distinct().all()]
        else:
            vals = []
    else:
        vals = [a for (a,) in db.query(Transaction.bank_account).filter(
            Transaction.partner == product, Transaction.side == side,
            Transaction.bank_account.isnot(None), Transaction.bank_account != "").distinct().all()]
    return sorted({v for v in vals if v})


@router.get("/catalog")
def download_catalog(db: Session = Depends(get_db),
                     current_user=Depends(require_permission("data_download"))):
    """What raw source data is downloadable — per product & side, with row count + date span."""
    products = []
    # Core-ledger partners: bank + internal both live in the Transaction table, split by `side`.
    seen = set()
    for (partner,) in db.query(Transaction.partner).distinct().all():
        if not partner or partner == "mixed" or partner in seen:
            continue
        seen.add(partner)
        sides = {}
        for side in ("bank", "internal"):
            base = db.query(Transaction).filter(Transaction.partner == partner, Transaction.side == side)
            sides[side] = _summ(base, Transaction.recon_date)
            sides[side]["accounts"] = _accounts_for(db, partner, side)
        if sides["bank"]["count"] or sides["internal"]["count"]:
            products.append({"key": partner, "label": partner.upper(), "group": "core", "sides": sides})
    # Module products (their own source tables).
    for pk, entry in _MODULE_SOURCES.items():
        sides = {}
        for side in ("bank", "internal"):
            model, date_col = entry[side]
            sides[side] = _summ(db.query(model), getattr(model, date_col))
            sides[side]["accounts"] = _accounts_for(db, pk, side)
        products.append({"key": pk, "label": entry["label"], "group": "module", "sides": sides})
    products.sort(key=lambda p: (p["group"] != "core", p["label"]))
    return {"products": products, "side_labels": _SIDE_LABEL}


def _resolve(product, side):
    """→ (Model, business_date_col, core_filter). core_filter is (partner, side) for the core
    ledger, else None (module tables need no extra filter)."""
    if side not in ("bank", "internal"):
        raise HTTPException(status_code=400, detail="side must be 'bank' or 'internal'")
    if product in _MODULE_SOURCES:
        model, date_col = _MODULE_SOURCES[product][side]
        return model, date_col, None
    return Transaction, _CORE_DATE_COL, (product, side)


def _build_export(db, product, side, date_from=None, date_to=None, account=None):
    """Query the raw source rows for one product+side (optionally one bank account) within
    [date_from, date_to] and render them to a CLEAN .xlsx. Returns (bytes, row_count).

    Core ledger: expand each row's ORIGINAL uploaded columns from `raw_data` (so the file reads
    like the actual bank statement / dump) and append a couple of readable recon fields, dropping
    the internal Transaction plumbing. Module tables are already typed columns — keep the business
    ones, drop bookkeeping + naive-UTC datetimes (timezone pact, behavior-contract #12)."""
    model, date_col, core = _resolve(product, side)
    q = db.query(model)
    if core is not None:
        partner, sidev = core
        q = q.filter(Transaction.partner == partner, Transaction.side == sidev)
        if account:
            q = q.filter(Transaction.bank_account == account)
    elif account and product == "evalue":
        q = q.filter(model.reco_acc_no == account)
    date_expr = getattr(model, date_col)
    if date_from:
        q = q.filter(date_expr >= date_from)
    if date_to:
        q = q.filter(date_expr <= date_to)
    rows = q.order_by(date_expr).all()

    if core is not None:
        records = []
        for r in rows:
            rec = {}
            try:
                loaded = json.loads(r.raw_data) if r.raw_data else {}
                if isinstance(loaded, dict):
                    rec = dict(loaded)
            except Exception:
                rec = {}
            # append readable recon context after the original columns
            rec["Recon Status"] = _status_label(r.recon_status)
            if r.match_id:
                rec["Match ID"] = r.match_id
            if r.bank_account:
                rec["Bank Account"] = r.bank_account
            records.append(rec)
        df = pd.DataFrame(records)
    else:
        cols = [c.name for c in model.__table__.columns
                if c.name not in _MODULE_DROP and not isinstance(c.type, DateTime)]
        df = pd.DataFrame([[getattr(r, c) for c in cols] for r in rows], columns=cols)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as xl:
        df.to_excel(xl, index=False, sheet_name="Data")
    return output.getvalue(), len(rows)


@router.get("/export")
def download_export(
    product: str,
    side: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    account: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("data_download")),
):
    """Download the ingested rows for one product + side (optionally one bank account), filtered
    by business date, as a clean statement-shaped .xlsx. Every download is audited."""
    content, n = _build_export(db, product, side, date_from, date_to, account)

    # Audit the pull BEFORE streaming (sensitive-data governance — real account numbers).
    try:
        db.add(AuditLog(user_id=current_user.id, username=current_user.username,
                        action="data_download", action_type="human",
                        entity_type="download", entity_id=f"{product}/{side}",
                        detail=json.dumps({"product": product, "side": side, "account": account,
                                           "date_from": date_from, "date_to": date_to, "rows": n})))
        db.commit()
    except Exception:
        db.rollback()

    acct_tag = (account or "").replace("/", "-").replace(" ", "")[:20]
    tag = "_".join(x for x in (product, side, acct_tag, date_from, date_to) if x)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{tag or product}.xlsx"'})
