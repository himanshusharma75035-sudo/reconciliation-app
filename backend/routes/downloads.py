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


def _summ(q, date_expr):
    # count over ALL rows; date span over REAL dates only, so the core ledger's `recon_date`
    # sentinels ('auto' / 'auto (multi-date)') don't leak into the displayed range.
    cnt = q.with_entities(func.count()).scalar() or 0
    mn, mx = q.with_entities(func.min(date_expr), func.max(date_expr)).filter(date_expr.like("20%")).one()
    return {"count": int(cnt), "min_date": mn, "max_date": mx}


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
        if sides["bank"]["count"] or sides["internal"]["count"]:
            products.append({"key": partner, "label": partner.upper(), "group": "core", "sides": sides})
    # Module products (their own source tables).
    for pk, entry in _MODULE_SOURCES.items():
        sides = {}
        for side in ("bank", "internal"):
            model, date_col = entry[side]
            sides[side] = _summ(db.query(model), getattr(model, date_col))
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


def _build_export(db, product, side, date_from=None, date_to=None):
    """Query the raw source rows for one product+side within [date_from, date_to] and render
    them to an .xlsx byte string. Returns (bytes, row_count). Dumps every business column
    as-is; skips ingestion-timestamp columns so we never emit a naive-UTC datetime the frontend
    would double-shift (timezone pact, behavior-contract #12)."""
    model, date_col, core = _resolve(product, side)
    q = db.query(model)
    if core is not None:
        partner, sidev = core
        q = q.filter(Transaction.partner == partner, Transaction.side == sidev)
    date_expr = getattr(model, date_col)
    if date_from:
        q = q.filter(date_expr >= date_from)
    if date_to:
        q = q.filter(date_expr <= date_to)
    rows = q.order_by(date_expr).all()

    cols = [c.name for c in model.__table__.columns if not isinstance(c.type, DateTime)]
    data = [[getattr(r, c) for c in cols] for r in rows]
    df = pd.DataFrame(data, columns=cols)
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
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("data_download")),
):
    """Download the RAW ingested rows (as stored) for one product + side, filtered by business
    date. Every download is audited."""
    content, n = _build_export(db, product, side, date_from, date_to)

    # Audit the pull BEFORE streaming (sensitive-data governance — real account numbers).
    try:
        db.add(AuditLog(user_id=current_user.id, username=current_user.username,
                        action="data_download", action_type="human",
                        entity_type="download", entity_id=f"{product}/{side}",
                        detail=json.dumps({"product": product, "side": side, "date_from": date_from,
                                           "date_to": date_to, "rows": n})))
        db.commit()
    except Exception:
        db.rollback()

    tag = "_".join(x for x in (product, side, date_from, date_to) if x)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{tag or product}.xlsx"'})
