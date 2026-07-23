import logging
logger = logging.getLogger("eko_recon")

"""
routes/aeps_settlement.py

AePS Settlement Reconciliation:
  - T Plus report  (settlement breakdown per batch)
  - Anomaly report (individual unsettled RRNs)
  - CIB report     (chargebacks + penalties)

Settlement formula (verified against real data):
  Settlement = Transaction Amount - 3Way Anomaly - TwoFa Total - CD Amount
"""

import io
import datetime
import json
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.orm import Session
from sqlalchemy import func

from models.database import (
    get_db, AepsSettlement, AepsAnomaly, AepsCIB, Transaction,
    ReconStatus, generate_id, AuditLog, PIIntegrityCheck
)
from core.auth import get_current_user, require_permission, require_product_access

router = APIRouter(prefix="/api/aeps", tags=["aeps-settlement"],
                   dependencies=[Depends(require_product_access("aeps"))])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(v, default=0.0):
    try:
        s = str(v).replace(',', '').strip()
        return float(s) if s.lower() not in ('', 'nan', 'none', 'null') else default
    except Exception:
        return default

def _safe_int(v, default=0):
    try:
        s = str(v).strip()
        return int(float(s)) if s.lower() not in ('', 'nan', 'none', 'null') else default
    except Exception:
        return default

def _normalise_date(v) -> str:
    """
    Convert any date string to YYYY-MM-DD for consistent storage and filtering.
    Handles:  DD-MM-YYYY, DD/MM/YYYY, YYYY-MM-DD, YYYY/MM/DD, pandas Timestamp
    Returns original string unchanged if it can't be parsed.
    """
    if v is None:
        return ''
    import re
    s = str(v).strip()
    if not s or s.lower() in ('nan', 'none', 'null', ''):
        return ''
    # Already YYYY-MM-DD
    if re.match(r'^\d{4}-\d{2}-\d{2}', s):
        return s[:10]
    # DD-MM-YYYY or DD/MM/YYYY
    m = re.match(r'^(\d{2})[-/](\d{2})[-/](\d{4})', s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    # Pandas Timestamp or datetime repr "2026-05-21 00:00:00"
    m2 = re.match(r'^(\d{4})[-/](\d{2})[-/](\d{2})', s)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    return s

def _read_excel_with_header(content: bytes) -> pd.DataFrame:
    """Read Excel where row 0 is header, rest is data."""
    df = pd.read_excel(io.BytesIO(content), header=None)
    if df.empty:
        raise ValueError("Empty file")
    header = [str(c).strip() for c in df.iloc[0].tolist()]
    data   = df.iloc[1:].reset_index(drop=True)
    data.columns = header
    return data


# ── T Plus Upload ─────────────────────────────────────────────────────────────

@router.post("/upload/tplus")
def upload_tplus(
    file: UploadFile = File(...),
    force: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """
    Upload Fingpay T Plus settlement report.
    One row per settlement batch; captures the complete deduction breakdown.
    """
    content = file.file.read()
    # SHA-256 duplicate guard + Re-upload (replace). Re-applying is idempotent: the
    # ingest below dedups on content keys, so a forced re-apply skips existing rows and
    # inserts only additional entries — replace-not-duplicate by construction.
    from core.file_hash_guard import guard_duplicate_file
    guard_duplicate_file(db, "aeps", "tplus", content, file.filename or "", current_user, force=force)
    try:
        df = _read_excel_with_header(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot parse file: {e}")

    inserted, duplicates = 0, 0
    errors   = []

    # Dedup: a settlement batch is uniquely identified by (reference number,
    # created date, settlement amount). Re-uploading the same T Plus report skips
    # already-present batches instead of duplicating the settlement totals.
    def _tkey(ref, cd, amt):
        return (str(ref or "").strip(), str(cd or "").strip(), round(float(amt or 0), 2))
    existing = set()
    for r in db.query(AepsSettlement.reference_number, AepsSettlement.created_date, AepsSettlement.settlement_amount).all():
        existing.add(_tkey(r[0], r[1], r[2]))

    for idx, row in df.iterrows():
        try:
            _ref = str(row.get("Reference Number", "")).strip()
            _cd  = _normalise_date(row.get("Created Date"))
            _sa  = _safe_float(row.get("Settlement Amount"))
            _k   = _tkey(_ref, _cd, _sa)
            if _k in existing:
                duplicates += 1
                continue
            existing.add(_k)
            entry = AepsSettlement(
                id                = generate_id(),
                upload_date       = str(datetime.date.today()),
                created_date      = _cd,
                settle_timestamp  = str(row.get("Settle Timestamp", "")).strip(),
                settlement_amount = _sa,
                transaction_amount= _safe_float(row.get("Transaction Amount")),
                anomaly_amount    = _safe_float(row.get("3Way Anomaly")),
                hold_recovery     = _safe_float(row.get("Hold Amount/Recovery")),
                twafa_per_txn     = _safe_float(row.get("TwoFa Charges")),
                twafa_count       = _safe_int(row.get("TwoFa Count")),
                twafa_total       = _safe_float(row.get("TwoFa Amount")),
                cd_amount         = _safe_float(row.get("CD Amount")),
                reference_number  = str(row.get("Reference Number", "")).strip(),
                cms_number        = str(row.get("CMS Number", "")).strip(),
                status_message    = str(row.get("Status Message", "")).strip(),
                service_type      = str(row.get("Service Type", "")).strip(),
                status            = str(row.get("Status", "")).strip(),
                uploaded_by       = current_user.username,
            )
            db.add(entry)
            inserted += 1
        except Exception as e:
            errors.append(f"Row {idx+2}: {e}")

    db.commit()

    # ── AS=SD cross-file trigger: update PIIntegrityCheck for each T Plus date ──
    # This is Item 2 of the AS=SD cross-file integrity feature.
    # For each settlement batch inserted, compare against the cw-success-failure
    # Fingpay file for the same date (if already uploaded).
    integrity_updates = []
    try:
        from routes.upload import _update_aeps_integrity_tplus
        # Re-read to get the entries we just inserted
        for idx, row in df.iterrows():
            created_date    = _normalise_date(row.get("Created Date"))
            txn_amount      = _safe_float(row.get("Transaction Amount"))
            if created_date and txn_amount:
                ic = _update_aeps_integrity_tplus(db, created_date, txn_amount)
                if ic:
                    integrity_updates.append(ic)
    except Exception as _e:
        logger.warning(f"routes/aeps_settlement.py: {_e}")  # Integrity check failure must never block T Plus ingestion

    _audit(db, current_user, "upload_tplus", file.filename, {"inserted": inserted, "duplicates": duplicates})
    return {
        "inserted": inserted,
        "duplicates": duplicates,
        "errors": errors,
        "filename": file.filename,
        "integrity_checks": integrity_updates,  # show AS=SD status per date
    }


# ── Anomaly Upload ────────────────────────────────────────────────────────────

@router.post("/upload/anomaly")
def upload_anomaly(
    file: UploadFile = File(...),
    force: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """
    Upload Fingpay Anomaly RRN report.
    Each row = one unsettled transaction; RRN links to bank tracking_number.
    """
    content = file.file.read()
    # SHA-256 duplicate guard + Re-upload (replace). Re-applying is idempotent: the
    # ingest below dedups on content keys, so a forced re-apply skips existing rows and
    # inserts only additional entries — replace-not-duplicate by construction.
    from core.file_hash_guard import guard_duplicate_file
    guard_duplicate_file(db, "aeps", "anomaly", content, file.filename or "", current_user, force=force)
    try:
        df = _read_excel_with_header(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot parse file: {e}")

    inserted, duplicates, errors = 0, 0, []

    # Dedup: an unsettled anomaly is uniquely identified by (RRN, amount, txn time).
    # Re-uploading the same Fingpay report — or the same RRN carried forward in a
    # later report — must NOT create a second row (which would double-count the
    # unsettled total). Such rows are SKIPPED and reported as duplicates.
    def _akey(rrn, amt, ts):
        return (str(rrn).strip(), round(float(amt or 0), 2), (ts or "").strip())
    existing = set()
    for r in db.query(AepsAnomaly.rrn, AepsAnomaly.amount, AepsAnomaly.txn_timestamp).all():
        existing.add(_akey(r[0], r[1], r[2]))

    for idx, row in df.iterrows():
        try:
            rrn = str(row.get("RRN", "")).strip()
            if not rrn:
                continue
            amount = _safe_float(row.get("Transaction Amount"))
            ts     = _normalise_date(row.get("Requested Timestamp")) or str(row.get("Requested Timestamp", "")).strip()
            key    = _akey(rrn, amount, ts)
            if key in existing:
                duplicates += 1
                continue
            existing.add(key)
            entry = AepsAnomaly(
                id                = generate_id(),
                upload_date       = str(datetime.date.today()),
                rrn               = rrn,
                amount            = amount,
                original_status   = str(row.get("Original Status", "")).strip(),
                threeway_status   = str(row.get("ThreeWay Status", "")).strip(),
                txn_timestamp     = ts,
                created_timestamp = _normalise_date(row.get("Created Timestamp")) or str(row.get("Created Timestamp", "")).strip(),
                service_type      = str(row.get("Service Type", "")).strip(),
                active_flag       = str(row.get("Active Flag", "")).strip(),
                uploaded_by       = current_user.username,
            )
            db.add(entry)
            inserted += 1
        except Exception as e:
            errors.append(f"Row {idx+2}: {e}")

    db.commit()
    _audit(db, current_user, "upload_anomaly", file.filename, {"inserted": inserted, "duplicates": duplicates})
    return {"inserted": inserted, "duplicates": duplicates, "errors": errors, "filename": file.filename}


# ── CIB Upload ────────────────────────────────────────────────────────────────

@router.post("/upload/cib")
def upload_cib(
    file: UploadFile = File(...),
    force: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """
    Upload Fingpay CIB (Chargeback & Penalty) report.
    Each row = one chargeback or penalty deduction; RRN links to bank tracking_number.
    """
    content = file.file.read()
    # SHA-256 duplicate guard + Re-upload (replace). Re-applying is idempotent: the
    # ingest below dedups on content keys, so a forced re-apply skips existing rows and
    # inserts only additional entries — replace-not-duplicate by construction.
    from core.file_hash_guard import guard_duplicate_file
    guard_duplicate_file(db, "aeps", "cib", content, file.filename or "", current_user, force=force)
    try:
        df = _read_excel_with_header(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot parse file: {e}")

    inserted, duplicates, errors = 0, 0, []

    # Normalise column names (CIB has some with leading/trailing spaces)
    df.columns = [c.strip() for c in df.columns]

    # Dedup: a chargeback/penalty is uniquely identified by (RRN, amount, txn date,
    # reason). Re-uploading the same CIB report skips already-present recoveries.
    def _ckey(rrn, amt, td, rsn):
        return (str(rrn).strip(), round(float(amt or 0), 2), str(td or "").strip(), str(rsn or "").strip())
    existing = set()
    for r in db.query(AepsCIB.rrn, AepsCIB.amount, AepsCIB.txn_date, AepsCIB.reason).all():
        existing.add(_ckey(r[0], r[1], r[2], r[3]))

    for idx, row in df.iterrows():
        try:
            rrn = str(row.get("RRN", "")).strip()
            if not rrn:
                continue
            amount   = _safe_float(row.get("Amount"))
            txn_date = _normalise_date(row.get("Transaction Date"))
            reason   = str(row.get("Reason", "")).strip()
            key      = _ckey(rrn, amount, txn_date, reason)
            if key in existing:
                duplicates += 1
                continue
            existing.add(key)
            entry = AepsCIB(
                id               = generate_id(),
                upload_date      = str(datetime.date.today()),
                recovery_date    = _normalise_date(row.get("Recovery Date")),
                rrn              = rrn,
                amount           = amount,
                anomaly_recovery = _safe_float(row.get("anomaly Recovery Amount")),
                product          = str(row.get("Product", "")).strip(),
                reason           = reason,
                dispute_type     = str(row.get("Dispute Type", "")).strip(),
                txn_date         = txn_date,
                uploaded_by      = current_user.username,
            )
            db.add(entry)
            inserted += 1
        except Exception as e:
            errors.append(f"Row {idx+2}: {e}")

    db.commit()
    _audit(db, current_user, "upload_cib", file.filename, {"inserted": inserted, "duplicates": duplicates})
    return {"inserted": inserted, "duplicates": duplicates, "errors": errors, "filename": file.filename}


# ── Settlement Summary ────────────────────────────────────────────────────────

@router.get("/settlement-summary")
def settlement_summary(
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Returns T Plus settlement records with formula verification.
    For each settlement: compares reported values against Anomaly + CIB totals.
    """
    q = db.query(AepsSettlement)
    if date_from: q = q.filter(AepsSettlement.created_date >= date_from)
    if date_to:   q = q.filter(AepsSettlement.created_date <= date_to)
    settlements = q.order_by(AepsSettlement.created_date.desc()).all()

    rows = []
    grand = {
        "transaction_amount": 0.0, "settlement_amount": 0.0,
        "anomaly_amount": 0.0, "twafa_total": 0.0, "cd_amount": 0.0,
    }

    for s in settlements:
        # Verify formula
        expected = round(s.transaction_amount - s.anomaly_amount - s.twafa_total - s.cd_amount, 2)
        diff     = round(s.settlement_amount - expected, 2)

        # Cross-check Anomaly report total for uploads on this date
        anomaly_uploaded = db.query(func.sum(AepsAnomaly.amount)).filter(
            AepsAnomaly.upload_date == s.upload_date
        ).scalar() or 0.0

        # Cross-check CIB total for uploads on this date
        cib_uploaded = db.query(func.sum(AepsCIB.amount)).filter(
            AepsCIB.upload_date == s.upload_date
        ).scalar() or 0.0

        for k in grand:
            grand[k] += getattr(s, k, 0) or 0

        rows.append({
            "id":                  s.id,
            "created_date":        s.created_date,
            "settle_timestamp":    s.settle_timestamp,
            "transaction_amount":  s.transaction_amount,
            "anomaly_amount":      s.anomaly_amount,
            "twafa_count":         s.twafa_count,
            "twafa_per_txn":       s.twafa_per_txn,
            "twafa_total":         s.twafa_total,
            "cd_amount":           s.cd_amount,
            "settlement_amount":   s.settlement_amount,
            "expected_settlement": expected,
            "diff":                diff,
            "formula_ok":          abs(diff) < 0.02,
            "anomaly_uploaded":    round(float(anomaly_uploaded), 2),
            "cib_uploaded":        round(float(cib_uploaded), 2),
            "anomaly_cross_ok":    abs(float(anomaly_uploaded) - s.anomaly_amount) < 0.02,
            "reference_number":    s.reference_number,
            "cms_number":          s.cms_number,
            "status":              s.status,
            "upload_date":         s.upload_date,
        })

    return {"rows": rows, "grand": grand}


# ── Anomaly List ──────────────────────────────────────────────────────────────

@router.get("/anomaly-list")
def anomaly_list(
    date_from:   Optional[str] = None,
    date_to:     Optional[str] = None,
    upload_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Returns Anomaly RRN entries cross-referenced against bank transactions.
    For each RRN: checks if corresponding bank txn exists in Transaction table.
    """
    q = db.query(AepsAnomaly)
    if upload_date: q = q.filter(AepsAnomaly.upload_date == upload_date)
    if date_from:   q = q.filter(AepsAnomaly.upload_date >= date_from)
    if date_to:     q = q.filter(AepsAnomaly.upload_date <= date_to)
    anomalies = q.order_by(AepsAnomaly.created_timestamp.desc()).all()

    rows = []
    total_amount = 0.0

    for a in anomalies:
        # Cross-reference RRN → bank transaction
        bank_txn = db.query(Transaction).filter(
            Transaction.tracking_number == a.rrn,
            Transaction.partner == "aeps",
            Transaction.side == "bank",
        ).first()

        total_amount += a.amount or 0

        rows.append({
            "id":               a.id,
            "upload_date":      a.upload_date,
            "rrn":              a.rrn,
            "amount":           a.amount,
            "original_status":  a.original_status,
            "threeway_status":  a.threeway_status,
            "txn_timestamp":    a.txn_timestamp,
            "created_timestamp":a.created_timestamp,
            "service_type":     a.service_type,
            "active_flag":      a.active_flag,
            # Bank cross-ref
            "bank_txn_found":   bank_txn is not None,
            "bank_eko_tid":     bank_txn.eko_tid     if bank_txn else None,
            "bank_amount":      bank_txn.amount       if bank_txn else None,
            "bank_recon_status":bank_txn.recon_status if bank_txn else None,
            "bank_txn_date":    bank_txn.transaction_date if bank_txn else None,
        })

    return {
        "rows":         rows,
        "total_amount": round(total_amount, 2),
        "count":        len(rows),
    }


# ── CIB List ──────────────────────────────────────────────────────────────────

@router.get("/cib-list")
def cib_list(
    date_from:   Optional[str] = None,
    date_to:     Optional[str] = None,
    upload_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Returns CIB chargeback/penalty entries cross-referenced against bank transactions.
    """
    q = db.query(AepsCIB)
    if upload_date: q = q.filter(AepsCIB.upload_date == upload_date)
    if date_from:   q = q.filter(AepsCIB.upload_date >= date_from)
    if date_to:     q = q.filter(AepsCIB.upload_date <= date_to)
    entries = q.order_by(AepsCIB.recovery_date.desc()).all()

    rows = []
    total_cb = total_penalty = 0.0

    for c in entries:
        # Cross-reference RRN → bank transaction
        bank_txn = db.query(Transaction).filter(
            Transaction.tracking_number == c.rrn,
            Transaction.partner == "aeps",
            Transaction.side == "bank",
        ).first()

        if "PENALTY" in (c.reason or "").upper():
            total_penalty += c.amount or 0
        else:
            total_cb += c.amount or 0

        rows.append({
            "id":               c.id,
            "upload_date":      c.upload_date,
            "recovery_date":    c.recovery_date,
            "rrn":              c.rrn,
            "amount":           c.amount,
            "anomaly_recovery": c.anomaly_recovery,
            "product":          c.product,
            "reason":           c.reason,
            "dispute_type":     c.dispute_type,
            "txn_date":         c.txn_date,
            # Bank cross-ref
            "bank_txn_found":   bank_txn is not None,
            "bank_eko_tid":     bank_txn.eko_tid        if bank_txn else None,
            "bank_amount":      bank_txn.amount          if bank_txn else None,
            "bank_recon_status":bank_txn.recon_status    if bank_txn else None,
        })

    return {
        "rows":          rows,
        "total_chargeback": round(total_cb, 2),
        "total_penalty":    round(total_penalty, 2),
        "total_deduction":  round(total_cb + total_penalty, 2),
        "count":            len(rows),
    }


# ── AS=SD Integrity Check list ────────────────────────────────────────────────

@router.get("/integrity-checks")
def list_integrity_checks(
    date_from:   Optional[str] = None,
    date_to:     Optional[str] = None,
    status:      Optional[str] = None,
    db: Session  = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """List AePS AS=SD cross-file integrity check states per date."""
    from routes.upload import _aeps_integrity_dict
    q = db.query(PIIntegrityCheck).filter(PIIntegrityCheck.partner == "aeps")
    if date_from: q = q.filter(PIIntegrityCheck.recon_date >= date_from)
    if date_to:   q = q.filter(PIIntegrityCheck.recon_date <= date_to)
    if status:    q = q.filter(PIIntegrityCheck.status == status)
    records = q.order_by(PIIntegrityCheck.recon_date.desc()).all()
    return {"rows": [_aeps_integrity_dict(r) for r in records], "count": len(records)}


# ── Clear settlement data ─────────────────────────────────────────────────────

@router.delete("/clear")
def clear_settlement(
    report_type: Optional[str] = Query(None, description="tplus | anomaly | cib | all"),
    upload_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    counts = {}
    if report_type in (None, "all", "tplus"):
        q = db.query(AepsSettlement)
        if upload_date: q = q.filter(AepsSettlement.upload_date == upload_date)
        counts["tplus"] = q.delete(); db.commit()
    if report_type in (None, "all", "anomaly"):
        q = db.query(AepsAnomaly)
        if upload_date: q = q.filter(AepsAnomaly.upload_date == upload_date)
        counts["anomaly"] = q.delete(); db.commit()
    if report_type in (None, "all", "cib"):
        q = db.query(AepsCIB)
        if upload_date: q = q.filter(AepsCIB.upload_date == upload_date)
        counts["cib"] = q.delete(); db.commit()
    return {"deleted": counts}


# ── Audit helper ──────────────────────────────────────────────────────────────

def _audit(db, user, action, filename, count):
    try:
        db.add(AuditLog(
            user_id     = user.id,
            username    = user.username,
            action      = action,
            entity_type = "aeps_settlement",
            entity_id   = None,
            action_type = "human",   # AePS settlement uploads are deliberate admin actions
            detail      = json.dumps({"filename": filename, "rows_inserted": count}),
        ))
        db.commit()
    except Exception:
        db.rollback()
