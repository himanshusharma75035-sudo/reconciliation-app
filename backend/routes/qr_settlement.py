import logging
logger = logging.getLogger("eko_recon")

"""
routes/qr_settlement.py

QR Collection — Settlement & Chargeback management

Settlement (ExportSettlementsList):
  Net Settlement = Amount − Fees − Fees Tax − Early Settlement Fees − Early Settlement Tax

Chargeback (manual entry from Paypoint email):
  Fields: case_date, adj_type, txn_date, upi_txn_id, rrn, adj_amount, tat_date
  RRN cross-references bank Transaction table (utr_number or tracking_number)
"""

import io
import json
import datetime
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Body
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from models.database import (
    get_db, QRSettlement, QRChargeback, Transaction,
    ReconStatus, generate_id, AuditLog
)
from core.auth import get_current_user, require_permission, require_product_access

router = APIRouter(prefix="/api/qr", tags=["qr-settlement"],
                   dependencies=[Depends(require_product_access("qr"))])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(v, default=0.0):
    try:
        s = str(v).replace(',', '').strip()
        return float(s) if s.lower() not in ('', 'nan', 'none', 'null') else default
    except Exception:
        return default

def _normalise_date(v) -> str:
    import re
    if v is None: return ''
    s = str(v).strip()
    if not s or s.lower() in ('nan', 'none', 'null', ''): return ''
    if re.match(r'^\d{4}-\d{2}-\d{2}', s): return s[:10]
    m = re.match(r'^(\d{2})[-/](\d{2})[-/](\d{4})', s)
    if m: return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m2 = re.match(r'^(\d{4})[-/](\d{2})[-/](\d{2})', s)
    if m2: return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    # Handle "28 Feb 2026 09:36 PM" style
    try:
        import dateutil.parser
        return str(dateutil.parser.parse(s).date())
    except Exception:
        return s

def _audit(db, user, action, detail, entity_id=None):
    try:
        db.add(AuditLog(
            user_id=user.id, username=user.username,
            action=action, entity_type="qr_settlement", entity_id=entity_id,
            detail=json.dumps(detail),
        ))
        db.commit()
    except Exception:
        db.rollback()


# ── Settlement Upload ─────────────────────────────────────────────────────────

@router.post("/upload/settlement")
def upload_settlement(
    file: UploadFile = File(...),
    force: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """
    Upload QR settlement report (ExportSettlementsList_*.xlsx).
    One row per settlement batch. Formula verified:
      Net = Amount - Fees - Fees Tax - Early Settlement Fees - Early Settlement Tax

    UPSERT per Settlement ID (was a pure append with no duplicate guard — re-uploading
    the same export silently DOUBLED every batch and the summary grand totals). A row
    whose Settlement ID already exists REPLACES the prior row; new batches insert; the
    SHA-256 guard blocks byte-identical re-uploads, with force= as the deliberate
    re-apply path (safe now that re-applying is idempotent).
    """
    content = file.file.read()
    from core.file_hash_guard import guard_duplicate_file
    guard_duplicate_file(db, "qr", "settlement", content, file.filename or "",
                         current_user, force=force)
    try:
        df = pd.read_excel(io.BytesIO(content), dtype=str)
        df.columns = [str(c).strip() for c in df.columns]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot parse file: {e}")

    inserted, replaced, errors = 0, 0, []

    # Excel text-format marker ('123 renders as 123) — artifact, never data. The
    # settlement_id is an UPSERT KEY and rrn a cross-ref to core transactions, so a
    # marker on either would duplicate batches / break the chargeback lookup.
    def _noq(v):
        s = str(v).strip() if v is not None else ""
        return s.lstrip("'").strip() if s.startswith("'") else s

    for idx, row in df.iterrows():
        try:
            _sid = _noq(row.get("Settlement ID", ""))
            if _sid:
                _ex = (db.query(QRSettlement)
                       .filter(QRSettlement.settlement_id == _sid).all())
                for _e_row in _ex:
                    db.delete(_e_row); replaced += 1
            gross  = _safe_float(row.get("Amount"))
            fees   = _safe_float(row.get("Fees"))
            f_tax  = _safe_float(row.get("Fees Tax Amount"))
            e_fees = _safe_float(row.get("Early Settlement Fees Amount"))
            e_tax  = _safe_float(row.get("EarlySettlement Tax Amount"))
            net    = _safe_float(row.get("Net Settlement Amount"))

            # Verify formula
            expected_net = round(gross - fees - f_tax - e_fees - e_tax, 2)
            diff = abs(expected_net - round(net, 2))
            if diff > 0.05:
                errors.append(f"Row {idx+2}: formula mismatch — expected {expected_net}, got {net}")

            db.add(QRSettlement(
                id                    = generate_id(),
                upload_date           = str(datetime.date.today()),
                settlement_id         = _sid,
                payment_date          = _normalise_date(row.get("Payment Date")),
                gross_amount          = gross,
                fees                  = fees,
                fees_tax              = f_tax,
                early_settlement_fees = e_fees,
                early_settlement_tax  = e_tax,
                net_settlement        = net,
                mode                  = str(row.get("Mode", "")).strip(),
                payment_status        = str(row.get("Payment Status", "")).strip(),
                payout_status         = str(row.get("Payout Status", "")).strip(),
                payout_remarks        = str(row.get("Payout Remarks", "")).strip(),
                bank_ref_number       = _noq(row.get("Bank Ref Number", "")),
                rrn                   = _noq(row.get("RRN", "")),
                uploaded_by           = current_user.username,
            ))
            inserted += 1
        except Exception as e:
            errors.append(f"Row {idx+2}: {e}")

    db.commit()
    _audit(db, current_user, "upload_qr_settlement",
           {"filename": file.filename, "rows_inserted": inserted, "replaced": replaced},
           entity_id=file.filename)
    return {"inserted": inserted, "replaced": replaced, "errors": errors,
            "filename": file.filename}


# ── Settlement Summary ────────────────────────────────────────────────────────

@router.get("/settlement-summary")
def settlement_summary(
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    q = db.query(QRSettlement)
    if date_from: q = q.filter(QRSettlement.payment_date >= date_from)
    if date_to:   q = q.filter(QRSettlement.payment_date <= date_to)
    settlements = q.order_by(QRSettlement.payment_date.desc()).all()

    rows = []
    grand = {"gross_amount": 0.0, "fees": 0.0, "fees_tax": 0.0,
             "early_settlement_fees": 0.0, "early_settlement_tax": 0.0,
             "net_settlement": 0.0}

    for s in settlements:
        expected_net = round(
            s.gross_amount - s.fees - s.fees_tax
            - s.early_settlement_fees - s.early_settlement_tax, 2
        )
        diff = round(s.net_settlement - expected_net, 2)

        for k in grand:
            grand[k] += getattr(s, k, 0) or 0

        rows.append({
            "id":                    s.id,
            "settlement_id":         s.settlement_id,
            "payment_date":          s.payment_date,
            "gross_amount":          s.gross_amount,
            "fees":                  s.fees,
            "fees_tax":              s.fees_tax,
            "early_settlement_fees": s.early_settlement_fees,
            "early_settlement_tax":  s.early_settlement_tax,
            "net_settlement":        s.net_settlement,
            "expected_net":          expected_net,
            "diff":                  diff,
            "formula_ok":            abs(diff) < 0.05,
            "mode":                  s.mode,
            "payment_status":        s.payment_status,
            "payout_status":         s.payout_status,
            "bank_ref_number":       s.bank_ref_number,
            "rrn":                   s.rrn,
            "upload_date":           s.upload_date,
        })

    return {"rows": rows, "grand": grand}


# ── Chargeback CRUD ───────────────────────────────────────────────────────────

class ChargebackIn(BaseModel):
    case_date:  str
    adj_type:   str
    txn_date:   str
    upi_txn_id: str = ""
    rrn:        str
    adj_amount: float
    tat_date:   str = ""
    status:     str = "pending"   # pending | settled | defended
    notes:      str = ""


@router.post("/chargebacks")
def create_chargeback(
    body: ChargebackIn,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """Create a chargeback entry (manual entry from Paypoint email)."""
    cb = QRChargeback(
        id          = generate_id(),
        case_date   = _normalise_date(body.case_date),
        adj_type    = body.adj_type,
        txn_date    = _normalise_date(body.txn_date),
        upi_txn_id  = body.upi_txn_id,
        rrn         = body.rrn.strip().lstrip("'").strip(),   # Excel marker guard — rrn cross-refs core txns
        adj_amount  = body.adj_amount,
        tat_date    = _normalise_date(body.tat_date),
        status      = body.status,
        notes       = body.notes,
        created_by  = current_user.username,
    )
    db.add(cb); db.commit()
    _audit(db, current_user, "create_qr_chargeback",
           {"rrn": body.rrn, "amount": body.adj_amount},
           entity_id=cb.id)
    return {"id": cb.id, "message": "Chargeback created"}


@router.get("/chargebacks")
def list_chargebacks(
    status:    Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List chargeback entries with cross-reference to bank transactions."""
    q = db.query(QRChargeback)
    if status:    q = q.filter(QRChargeback.status == status)
    if date_from: q = q.filter(QRChargeback.case_date >= date_from)
    if date_to:   q = q.filter(QRChargeback.case_date <= date_to)
    chargebacks = q.order_by(QRChargeback.case_date.desc()).all()

    rows = []
    total_pending  = 0.0
    total_settled  = 0.0

    for cb in chargebacks:
        # Cross-reference RRN → bank transaction
        bank_txn = db.query(Transaction).filter(
            Transaction.partner == "qr",
            Transaction.side == "bank",
            Transaction.utr_number == cb.rrn,
        ).first()
        # Also try tracking_number if utr_number didn't match
        if not bank_txn:
            bank_txn = db.query(Transaction).filter(
                Transaction.partner == "qr",
                Transaction.side == "bank",
                Transaction.tracking_number == cb.rrn,
            ).first()

        if cb.status == "pending": total_pending += cb.adj_amount or 0
        else: total_settled += cb.adj_amount or 0

        rows.append({
            "id":         cb.id,
            "case_date":  cb.case_date,
            "adj_type":   cb.adj_type,
            "txn_date":   cb.txn_date,
            "upi_txn_id": cb.upi_txn_id,
            "rrn":        cb.rrn,
            "adj_amount": cb.adj_amount,
            "tat_date":   cb.tat_date,
            "status":     cb.status,
            "notes":      cb.notes,
            "created_by": cb.created_by,
            "created_at": str(cb.created_at),
            # Bank cross-ref
            "bank_txn_found":    bank_txn is not None,
            "bank_eko_tid":      bank_txn.eko_tid        if bank_txn else None,
            "bank_amount":       bank_txn.amount          if bank_txn else None,
            "bank_recon_status": bank_txn.recon_status    if bank_txn else None,
            "bank_txn_date":     bank_txn.transaction_date if bank_txn else None,
        })

    return {
        "rows": rows,
        "total_pending": round(total_pending, 2),
        "total_settled": round(total_settled, 2),
        "count": len(rows),
    }


@router.put("/chargebacks/{cb_id}")
def update_chargeback(
    cb_id: str,
    body: ChargebackIn,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    cb = db.query(QRChargeback).filter(QRChargeback.id == cb_id).first()
    if not cb:
        raise HTTPException(status_code=404, detail="Chargeback not found")
    for field, val in body.dict().items():
        if field in ("case_date", "txn_date", "tat_date"):
            val = _normalise_date(val)
        setattr(cb, field, val)
    db.commit()
    return {"message": "Updated"}


@router.delete("/chargebacks/{cb_id}")
def delete_chargeback(
    cb_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    cb = db.query(QRChargeback).filter(QRChargeback.id == cb_id).first()
    if not cb:
        raise HTTPException(status_code=404, detail="Chargeback not found")
    # Soft delete via the recycle bin (was a hard db.delete — financial rows must stay
    # recoverable, ground rule + skills.md).
    from core import recycle_bin
    q = db.query(QRChargeback).filter(QRChargeback.id == cb_id)
    recycle_bin.soft_delete(db, q, QRChargeback, module="qr",
                            user=current_user.username, filters={"id": cb_id},
                            reason="delete chargeback")
    db.commit()
    return {"message": "Deleted (recoverable from the Recycle Bin)"}


# ── Clear data ────────────────────────────────────────────────────────────────

@router.delete("/clear")
def clear_qr_data(
    report_type: Optional[str] = Query(None, description="settlement | chargebacks | all"),
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    counts = {}
    if report_type in (None, "all", "settlement"):
        counts["settlement"] = db.query(QRSettlement).delete(); db.commit()
    if report_type in (None, "all", "chargebacks"):
        counts["chargebacks"] = db.query(QRChargeback).delete(); db.commit()
    return {"deleted": counts}
