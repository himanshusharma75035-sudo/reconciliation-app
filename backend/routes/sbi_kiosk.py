import logging
logger = logging.getLogger("eko_recon")

"""
routes/sbi_kiosk.py

SBI Kiosk Banking — Full Reconciliation Module (4 Processes)

P01 — SBI Settlement Reconciliation
   KO Limits Config (KO Withdrawal) ↔ Bank Statement (EKOSETTLEMENT)
   Match per KO ID; surface CREDITED / PENDING / PARTIAL

P02 — Bank Statement & Transaction Report Reconciliation
   7 Transaction Report files ↔ Bank Statement
   Match on 20-digit Reference Number; detect Reversals (same ref, DR+CR)

P03 — CSP-Transaction-Bank Reconciliation
   CSP Master + Transaction Report (money OUT) ↔ Bank Statement (money IN)
   CSP Code + Amount with D+1 / D-1 date-shift logic (4-priority)

P04 — CSP Wallet Balance Reconciliation
   Limit Update Failure Report ↔ KO Cash Holding
   Identify under/overfunded wallets; track DEPOSIT / WITHDRAWAL actions
"""

import io
import re
import json
import datetime
from typing import Optional, List

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from models.database import (
    get_db, generate_id, AuditLog,
    SBIBankTransaction, SBITxnReport, SBIKOLimits,
    SBIKOCashHolding, SBILimitFailure, SBICSPMaster,
    SBIP01Result, SBIP02Result, SBIP03Result, SBIP04Result,
    SBIManualMatch, SBISrcAssignment,
)
from core.auth import get_current_user, require_permission

router = APIRouter(prefix="/api/sbi", tags=["sbi-kiosk"])

# ── Helpers ───────────────────────────────────────────────────────────────────

def _clip(v, n: int) -> str:
    """Clip a string to a column's max length. MySQL ENFORCES VARCHAR lengths
    (SQLite ignores them), so an over-long parsed value — e.g. a settlement
    description leaking into ref_number — fails the whole upload on prod with
    'Data too long for column …'. Clipping at ingest makes uploads robust on both."""
    s = '' if v is None else str(v)
    return s[:n] if n and len(s) > n else s


def _sf(v, default=0.0) -> float:
    try:
        s = str(v).replace(',', '').strip()
        return float(s) if s and s.lower() not in ('nan', 'none', '') else default
    except Exception:
        return default

def _clean(v) -> str:
    s = str(v).strip() if v is not None else ''
    return '' if s.lower() in ('nan', 'none') else s

def _nd(v) -> str:
    """Normalise date string to YYYY-MM-DD."""
    import re as _re
    s = _clean(v)
    if not s: return ''
    # DD/MM/YYYY or DD-MM-YYYY
    m = _re.match(r'^(\d{2})[-/](\d{2})[-/](\d{4})', s)
    if m: return f'{m.group(3)}-{m.group(2)}-{m.group(1)}'
    # YYYY-MM-DD already
    if _re.match(r'^\d{4}-\d{2}-\d{2}', s): return s[:10]
    # DD-Mon-YYYY (e.g. 30-May-2026)
    try:
        import dateutil.parser
        return str(dateutil.parser.parse(s).date())
    except Exception:
        return s

def _extract_bank_ref(desc: str) -> str:
    """Extract 20-digit reference number from bank description."""
    m = re.search(r'(?:TO|BY)\s+TRANSFER-(\d{20})', desc)
    return m.group(1) if m else ''

def _extract_ko_id(desc: str) -> str:
    """Extract KO ID from bank description (after TXN@KO or @KO )."""
    m = re.search(r'TXN@KO\s*(\w+)[-\s]', desc)
    if m: return m.group(1)
    m = re.search(r'@KO\s*(\w+)', desc)
    return m.group(1) if m else ''

def _extract_settlement_info(desc: str):
    """
    For EKOSETTLEMENT rows, extract:
    - KO ID from: EKO DEDUCTION-{KO_ID}.EKOSETTLEMENT
    - Deduction date from: EKO DEDUCTION_{DD-MM-YYYY}
    Returns (ko_id, deduct_date)
    """
    ko_m = re.search(r'EKO DEDUCTION-(\w+)\.EKOSETTLEMENT', desc, re.I)
    dt_m = re.search(r'EKO[_ ]DEDUCTION[_-](\d{2}-\d{2}-\d{4})', desc, re.I)
    ko_id = ko_m.group(1) if ko_m else ''
    deduct_date = _nd(dt_m.group(1)) if dt_m else ''
    return ko_id, deduct_date

def _extract_txn_type_from_bank(desc: str) -> str:
    """Derive transaction type label from bank description."""
    desc_u = desc.upper()
    if 'EKOSETTLEMENT' in desc_u or 'EKO DEDUCTION' in desc_u: return 'Settlement'
    if 'AEPSOFFUSWDL' in desc_u: return 'AEPS OFFUS Withdrawal'
    if 'AEPSWDL' in desc_u: return 'AEPS Withdrawal'
    if 'AEPSDEPOSIT' in desc_u: return 'AEPS Deposit'
    if 'MONEYTRF' in desc_u: return 'Money Transfer'
    if 'WITHDRAWAL' in desc_u: return 'Withdrawal'
    if 'LOAN' in desc_u: return 'Loan'
    if 'RUPAYWDL' in desc_u: return 'Rupay Withdrawal'
    if 'INITIAL' in desc_u: return 'Initial Deposit'
    return 'Other'

def _audit(db: Session, user, action: str, detail: dict, action_type: str = "human"):
    # SBI uploads + P01–P04 runs are deliberate admin actions → "human" (not "app").
    try:
        db.add(AuditLog(
            user_id=user.id, username=user.username,
            action=action, entity_type="sbi_kiosk", entity_id=None,
            action_type=action_type,
            detail=json.dumps(detail),
        ))
        db.commit()
    except Exception:
        db.rollback()


# ── P01: Bank Statement Upload ────────────────────────────────────────────────

@router.post("/upload/bank-statement")
async def upload_bank_statement(
    file: UploadFile = File(...),
    recon_date: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """
    Upload SBI Bank Statement (settlement account, tab-separated .xls).
    Parses all transactions; auto-detects settlement rows via EKOSETTLEMENT keyword.
    Extracts KO ID and deduction date from settlement description.
    """
    content = (await file.read()).decode('utf-8', errors='replace')
    lines = content.splitlines()

    # Find header row
    header_idx = next((i for i, l in enumerate(lines) if 'Txn Date' in l and 'Description' in l), None)
    if header_idx is None:
        raise HTTPException(status_code=400, detail="Cannot find bank statement header row. Expected 'Txn Date' column.")

    inserted = 0
    errors = []
    today = str(datetime.date.today())
    _fund_rows = []   # funds-position lines (file order; reporting only)

    for line in lines[header_idx + 1:]:
        parts = line.strip().split('\t')
        if len(parts) < 6: continue
        try:
            txn_date   = _nd(parts[0].strip()) if parts else ''
            value_date = _nd(parts[1].strip()) if len(parts) > 1 else ''
            desc       = parts[2].strip() if len(parts) > 2 else ''
            ref_no     = parts[3].strip() if len(parts) > 3 else ''
            branch     = parts[4].strip() if len(parts) > 4 else ''
            debit      = _sf(parts[5].strip()) if len(parts) > 5 else 0.0
            credit     = _sf(parts[6].strip()) if len(parts) > 6 else 0.0
            balance    = _sf(parts[7].strip()) if len(parts) > 7 else None

            if not txn_date and not desc: continue

            is_settlement = bool(re.search(r'EKOSETTLEMENT|EKO.DEDUCTION', desc, re.I))
            ko_id_extracted, deduct_date = ('', '')
            if is_settlement:
                ko_id_extracted, deduct_date = _extract_settlement_info(desc)
            else:
                ko_id_extracted = _extract_ko_id(desc)

            ref_extracted = _extract_bank_ref(desc)

            db.add(SBIBankTransaction(
                id            = generate_id(),
                upload_date   = today,
                txn_date      = _clip(txn_date, 10),
                value_date    = _clip(value_date, 10),
                description   = desc,                            # Text — no length cap
                ref_number    = _clip(ref_extracted or ref_no, 100),
                branch_code   = _clip(branch, 20),
                debit         = debit,
                credit        = credit,
                balance       = balance,
                ko_id         = _clip(ko_id_extracted, 20),
                deduct_date   = _clip(deduct_date, 10),
                is_settlement = is_settlement,
                txn_type      = _clip(_extract_txn_type_from_bank(desc), 30),
            ))
            inserted += 1
            # Funds-position line (file order; reporting only)
            _fund_rows.append({"date": _nd(txn_date), "balance": balance,
                               "dr": debit or 0.0, "cr": credit or 0.0})
        except Exception as e:
            errors.append(f"Line error: {e}")

    db.commit()

    # ── Funds-position: EOD balance snapshots for the SBI settlement account ──
    try:
        from core.funds import record_snapshots
        record_snapshots(db, "kiosk", "sbi", "SBI settlement a/c", _fund_rows,
                         uploaded_by=current_user.username)
    except Exception as _e:
        logger.warning(f"routes/sbi_kiosk.py: funds snapshot skipped: {_e}")

    # M10: validate that we parsed a meaningful number of rows
    # SBI bank statements for a full day typically have hundreds of rows.
    # If inserted < 10, it likely means the file format changed or header detection failed.
    validation_warning = None
    if inserted < 10:
        validation_warning = (
            f"Only {inserted} rows were parsed. The file may have a different format "
            f"than expected. Verify the file is a valid SBI bank statement "
            f"(tab-separated with 'Txn Date' header row)."
        )
        logger.warning(f"SBI bank statement: low row count ({inserted}) for {file.filename}")

    _audit(db, current_user, "sbi_upload_bank_statement",
           {"filename": file.filename, "inserted": inserted, "warning": validation_warning})
    settlement_count = db.query(SBIBankTransaction).filter(
        SBIBankTransaction.upload_date == today,
        SBIBankTransaction.is_settlement == True
    ).count()
    auto_recon = _auto_run_after_upload(db, current_user)
    return {
        "inserted": inserted,
        "settlement_rows": settlement_count,
        "errors": errors[:10],
        "filename": file.filename,
        "validation_warning": validation_warning,
        "auto_recon": auto_recon,
    }


# ── P01: KO Limits Config Upload ──────────────────────────────────────────────

@router.post("/upload/ko-limits")
async def upload_ko_limits(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """
    Upload KO Limits Configuration Report (real Excel, 3 metadata rows then header).
    Records every KO Deposit and KO Withdrawal.
    """
    content = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(content), engine='xlrd', header=None)
    except Exception:
        try:
            df = pd.read_excel(io.BytesIO(content), header=None)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Cannot parse file: {e}")

    # Header is row 3 (0-indexed)
    h = [_clean(c) for c in df.iloc[3].tolist()]
    data = df.iloc[4:].copy()
    data.columns = h
    today = str(datetime.date.today())
    inserted = 0

    for _, row in data.iterrows():
        txn_type = _clean(row.get('Type of Transaction', ''))
        if txn_type not in ('KO Deposit', 'KO Withdrawal'): continue
        try:
            raw_dt = _clean(row.get('Date of Transaction', ''))
            db.add(SBIKOLimits(
                id                  = generate_id(),
                upload_date         = today,
                txn_datetime        = _clip(raw_dt, 30),
                txn_date            = _clip(_nd(raw_dt[:10] if raw_dt else ''), 10),
                limit_configured_by = _clip(_clean(row.get('Limit Configured By', '')), 20),
                ko_id               = _clip(_clean(row.get('KO ID', '')), 20),
                opening_limit       = _sf(row.get('Opening Limit')),
                txn_type            = _clip(txn_type, 30),
                amount              = _sf(row.get('Amount')),
                closing_limit       = _sf(row.get('Closing Limit')),
            ))
            inserted += 1
        except Exception as _e:
            logger.warning(f"routes/sbi_kiosk.py: {_e}")  # db.commit()
    _audit(db, current_user, "sbi_upload_ko_limits", {"filename": file.filename, "inserted": inserted})
    return {"inserted": inserted, "filename": file.filename,
            "auto_recon": _auto_run_after_upload(db, current_user)}


# ── P02: Transaction Report Upload ────────────────────────────────────────────

@router.post("/upload/txn-report")
async def upload_txn_report(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """
    Upload one of the 7 BC Transaction Report files.
    All files share the same column structure (3 metadata rows + header row 4).
    'Other Txn' file has extra REVERSAL_STATUS, SETTELMENT_ACCOUNT_, KO HOLDING columns.
    """
    content = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(content), engine='xlrd', header=None)
    except Exception:
        try:
            df = pd.read_excel(io.BytesIO(content), header=None)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Cannot parse file: {e}")

    # Header is row 3
    raw_h = df.iloc[3].tolist()
    h = [_clean(c) for c in raw_h]
    data = df.iloc[4:].copy()
    data.columns = h

    # Normalize column names (some files have empty cols mixed in)
    col_map = {c.lower().replace(' ', '_').replace('/', '_'): c for c in h if c}
    def gc(key): return col_map.get(key, '')
    def gv(row, key): return _clean(row.get(gc(key), '')) if gc(key) else ''

    today = str(datetime.date.today())
    source = file.filename
    inserted = 0

    for _, row in data.iterrows():
        # Skip metadata/empty rows
        sr = gv(row, 'sr._no') or gv(row, 'sr._no.')
        if not sr or not str(sr).replace('.', '').strip().isdigit(): continue

        raw_dt = gv(row, 'transaction_date_&_time') or gv(row, 'transaction_date')
        txn_date = _nd(raw_dt[:10]) if raw_dt else ''

        try:
            db.add(SBITxnReport(
                id              = generate_id(),
                upload_date     = today,
                source_file     = _clip(source, 100),
                ko_id           = _clip(gv(row, 'ko_id'), 20),
                txn_datetime    = _clip(raw_dt, 30),
                txn_date        = _clip(txn_date, 10),
                reference_number= _clip(gv(row, 'reference_number'), 100),
                txn_type        = _clip(gv(row, 'type_of_transaction'), 80),
                from_account    = _clip(gv(row, 'from_account'), 30),
                to_account      = _clip(gv(row, 'to_account'), 30),
                amount          = _sf(row.get(gc('amount'), 0)),
                customer_charge = _sf(row.get(gc('customer_charge'), 0)),
                journal_number  = _clip(gv(row, 'journal_number'), 30),
                status          = _clip(gv(row, 'status'), 20),
                reversal_status = _clip(gv(row, 'reversal_status'), 20),
                settlement_acct = _clip(gv(row, 'settelment_account_'), 20),
                ko_holding      = _sf(row.get(gc('ko_holding'), None)),
            ))
            inserted += 1
        except Exception as _e:
            logger.warning(f"routes/sbi_kiosk.py: {_e}")  # db.commit()
    _audit(db, current_user, "sbi_upload_txn_report", {"filename": source, "inserted": inserted})
    return {"inserted": inserted, "filename": source,
            "auto_recon": _auto_run_after_upload(db, current_user)}


# ── P03: CSP Master Sheet Upload ──────────────────────────────────────────────

@router.post("/upload/csp-master")
async def upload_csp_master(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """
    Upload CSP Master Sheet (CSP Code | Ref Number | Mode).
    Cash: fixed ref per CSP (same ref across multiple deposits).
    Electronic/CDM: each row is a unique transaction reference.
    """
    content = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(content), header=None)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot parse: {e}")

    # Header in row 0
    h = [_clean(c) for c in df.iloc[0].tolist()]
    data = df.iloc[1:].copy()
    data.columns = h
    today = str(datetime.date.today())
    inserted = 0

    for _, row in data.iterrows():
        csp = _clean(row.get('CSP Code', ''))
        if not csp: continue
        db.add(SBICSPMaster(
            id          = generate_id(),
            upload_date = today,
            csp_code    = _clip(csp, 20),
            ref_number  = _clip(_clean(row.get('Ref.. Number', '') or row.get('Ref Number', '')), 100),
            mode        = _clip(_clean(row.get('Mood', '') or row.get('Mode', '')), 30),
        ))
        inserted += 1

    db.commit()
    _audit(db, current_user, "sbi_upload_csp_master", {"filename": file.filename, "inserted": inserted})
    return {"inserted": inserted, "filename": file.filename,
            "auto_recon": _auto_run_after_upload(db, current_user)}


# ── P04: KO Cash Holding Upload ───────────────────────────────────────────────

@router.post("/upload/ko-cash-holding")
async def upload_ko_cash_holding(
    file: UploadFile = File(...),
    report_date: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """Upload KO Cash Holding Report (KO ID + opening/closing balances)."""
    content = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(content), engine='xlrd', header=None)
    except Exception:
        df = pd.read_excel(io.BytesIO(content), header=None)

    h = [_clean(c) for c in df.iloc[3].tolist()]
    data = df.iloc[4:].copy()
    data.columns = h
    today = str(datetime.date.today())
    r_date = _nd(report_date) if report_date else today
    inserted = 0

    for _, row in data.iterrows():
        ko = _clean(row.get('KO ID', ''))
        if not ko: continue
        db.add(SBIKOCashHolding(
            id              = generate_id(),
            upload_date     = today,
            report_date     = _clip(r_date, 10),
            ko_id           = _clip(ko, 20),
            limit           = _sf(row.get('Limit')),
            opening_balance = _sf(row.get('Opening Balance')),
            cash_receipts   = _sf(row.get('Cash Receipts')),
            cash_payments   = _sf(row.get('Cash Payments')),
            ko_deposit      = _sf(row.get('KO Deposit')),
            ko_withdrawal   = _sf(row.get('KO Withdrawal')),
            closing_balance = _sf(row.get('Closing Balance')),
        ))
        inserted += 1

    db.commit()
    _audit(db, current_user, "sbi_upload_ko_cash_holding", {"filename": file.filename, "inserted": inserted})
    return {"inserted": inserted, "filename": file.filename,
            "auto_recon": _auto_run_after_upload(db, current_user)}


@router.post("/upload/limit-failures")
async def upload_limit_failures(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """Upload Limit Update Failure Report (CSPs whose wallet limit update failed)."""
    content = await file.read()
    try:
        import xlrd
        book = xlrd.open_workbook(file_contents=content)
        sh = book.sheet_by_index(0)
        rows = [[str(sh.cell_value(r, c)).strip() for c in range(sh.ncols)] for r in range(sh.nrows)]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot parse Limit Fail file: {e}")

    # Header is row 3 (0-indexed)
    if len(rows) < 4:
        raise HTTPException(status_code=400, detail="File has too few rows")

    today = str(datetime.date.today())
    inserted = 0

    for row in rows[4:]:  # data starts at row 4
        if len(row) < 5: continue
        sr = row[0].replace('.', '').strip()
        if not sr or not sr.isdigit(): continue
        try:
            db.add(SBILimitFailure(
                id          = generate_id(),
                upload_date = today,
                txn_date    = _clip(_nd(row[1]), 10),
                csp_code    = _clip(row[2].strip(), 20),
                bc_id       = _clip(row[3].strip(), 20),
                amount      = _sf(row[4]),
                user        = _clip(row[5].strip() if len(row) > 5 else '', 50),
            ))
            inserted += 1
        except Exception as _e:
            logger.warning(f"routes/sbi_kiosk.py: {_e}")  # db.commit()
    _audit(db, current_user, "sbi_upload_limit_failures", {"filename": file.filename, "inserted": inserted})
    return {"inserted": inserted, "filename": file.filename,
            "auto_recon": _auto_run_after_upload(db, current_user)}


# ── P01: Run Settlement Reconciliation ────────────────────────────────────────

@router.post("/run/p01")
def run_p01(
    recon_date: str,
    upload_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("run_recon")),
):
    """
    P01 — SBI Settlement Reconciliation.
    Compares KO wallet withdrawals (KO Limits Config) against
    bank settlement credits (EKOSETTLEMENT rows in bank statement).
    Match key: KO ID. Handles D+1 via deduction_date field.
    Statuses: CREDITED | PENDING | PARTIAL | EXCESS
    """
    today = upload_date or str(datetime.date.today())

    # Clear old P01 results for this recon_date
    db.query(SBIP01Result).filter(SBIP01Result.recon_date == recon_date).delete()
    db.commit()

    # Load KO Limits — KO Withdrawals (money leaving CSP wallet = settlement request)
    ko_wdl_q = db.query(SBIKOLimits).filter(
        SBIKOLimits.txn_type == 'KO Withdrawal',
        SBIKOLimits.upload_date == today,
    )
    ko_withdrawals = {}  # ko_id → total amount withdrawn
    ko_dates = {}        # ko_id → txn_date
    for r in ko_wdl_q.all():
        ko_withdrawals[r.ko_id] = ko_withdrawals.get(r.ko_id, 0) + (r.amount or 0)
        ko_dates[r.ko_id] = r.txn_date

    # Load bank settlement transactions (EKOSETTLEMENT filter)
    # Include D+1: deductions from yesterday may appear in today's bank statement
    bank_settle_q = db.query(SBIBankTransaction).filter(
        SBIBankTransaction.is_settlement == True,
        SBIBankTransaction.upload_date == today,
    )
    bank_by_ko = {}      # ko_id → total bank settlement debit
    bank_dates = {}      # ko_id → bank txn_date
    bank_deduct = {}     # ko_id → wallet deduct_date (from description)
    for r in bank_settle_q.all():
        ko = r.ko_id
        if not ko: continue
        bank_by_ko[ko] = bank_by_ko.get(ko, 0) + (r.debit or 0)
        bank_dates[ko] = r.txn_date
        bank_deduct[ko] = r.deduct_date

    # All KOs that appear in either source
    all_kos = set(ko_withdrawals.keys()) | set(bank_by_ko.keys())
    results = []

    for ko in all_kos:
        wallet_amt = ko_withdrawals.get(ko, 0)
        bank_amt   = bank_by_ko.get(ko, 0)
        diff       = bank_amt - wallet_amt

        if wallet_amt == 0 and bank_amt > 0:
            status = 'EXCESS'          # bank credit with no wallet withdrawal (manual/error)
        elif wallet_amt > 0 and bank_amt == 0:
            status = 'PENDING'         # wallet withdrawn but no bank credit yet
        elif abs(diff) < 0.01:
            status = 'CREDITED'        # perfect match
        else:
            status = 'PARTIAL'         # amounts differ

        result = SBIP01Result(
            id              = generate_id(),
            recon_date      = recon_date,
            ko_id           = ko,
            wallet_withdrawn= wallet_amt,
            bank_settled    = bank_amt,
            difference      = round(diff, 2),
            status          = status,
            deduct_date     = bank_deduct.get(ko, ''),
            bank_txn_date   = bank_dates.get(ko, ''),
            notes           = (
                f"D+1 settlement (wallet deducted {bank_deduct.get(ko, '')}, settled {bank_dates.get(ko, '')})"
                if bank_deduct.get(ko) and bank_deduct.get(ko) != bank_dates.get(ko) else ''
            ),
        )
        db.add(result)
        results.append(result)

    db.commit()
    summary = {s: sum(1 for r in results if r.status == s) for s in ('CREDITED', 'PENDING', 'PARTIAL', 'EXCESS')}
    _audit(db, current_user, "sbi_run_p01", {"recon_date": recon_date, **summary})
    return {"recon_date": recon_date, "total_kos": len(results), "summary": summary}


# ── P02: Run Bank + Transaction Report Reconciliation ─────────────────────────

@router.post("/run/p02")
def run_p02(
    recon_date: str,
    upload_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("run_recon")),
):
    """
    P02 — Bank Statement & Transaction Report Reconciliation.
    Primary match: Reference Number (20-digit, extracted from bank description).
    Reversal: same ref appears as both DR and CR → tagged as Reversal Debit / Credit.
    Statuses: Matched / Unmatched / Partial / Reversal
    """
    today = upload_date or str(datetime.date.today())
    db.query(SBIP02Result).filter(SBIP02Result.recon_date == recon_date).delete()
    db.commit()

    # Build reference number index from all Transaction Reports
    txn_by_ref = {}  # ref → list of SBITxnReport rows
    for r in db.query(SBITxnReport).filter(SBITxnReport.upload_date == today).all():
        if r.reference_number:
            txn_by_ref.setdefault(r.reference_number, []).append(r)

    # Load all bank transactions
    bank_txns = db.query(SBIBankTransaction).filter(
        SBIBankTransaction.upload_date == today
    ).all()

    # First pass: detect reversals (same ref, DR + CR pair)
    ref_bank_map = {}  # ref → list of (bank_txn, type)
    for bt in bank_txns:
        if not bt.ref_number: continue
        ref_bank_map.setdefault(bt.ref_number, []).append(bt)

    reversal_refs = {
        ref for ref, txns in ref_bank_map.items()
        if any(t.debit > 0 for t in txns) and any(t.credit > 0 for t in txns)
    }

    results = []
    used_txn_ids = set()

    for bt in bank_txns:
        ref = bt.ref_number
        bank_type = 'DR' if bt.debit > 0 else 'CR'
        bank_amount = bt.debit if bt.debit > 0 else bt.credit

        # Reversal detection
        is_reversal = ref in reversal_refs
        reversal_type = ''
        if is_reversal:
            reversal_type = 'Reversal Debit' if bank_type == 'DR' else 'Reversal Credit'

        # Match against transaction reports
        matched_txn = None
        if ref and ref in txn_by_ref:
            for txn in txn_by_ref[ref]:
                if txn.id not in used_txn_ids:
                    matched_txn = txn
                    used_txn_ids.add(txn.id)
                    break

        if is_reversal:
            match_status = 'Reversal'
            success = 'Success' if matched_txn else 'Fail'
        elif matched_txn:
            # Validate amount
            amt_diff = abs(bank_amount - (matched_txn.amount or 0))
            match_status = 'Matched' if amt_diff < 1.0 else 'Partial'
            success = matched_txn.status or 'Success'
        elif not ref:
            match_status = 'Unmatched'
            success = 'Fail'
        else:
            match_status = 'Unmatched'
            success = 'Fail'

        result = SBIP02Result(
            id               = generate_id(),
            recon_date       = recon_date,
            bank_txn_id      = bt.id,
            txn_report_id    = matched_txn.id if matched_txn else None,
            reference_number = ref,
            ko_id            = bt.ko_id or (matched_txn.ko_id if matched_txn else ''),
            bank_amount      = bank_amount,
            bank_type        = bank_type,
            report_amount    = matched_txn.amount if matched_txn else None,
            report_txn_type  = matched_txn.txn_type if matched_txn else '',
            match_status     = match_status,
            reversal_type    = reversal_type,
            success_status   = success,
            notes            = bt.txn_type,
        )
        db.add(result)
        results.append(result)

    db.commit()
    summary = {s: sum(1 for r in results if r.match_status == s)
               for s in ('Matched', 'Unmatched', 'Partial', 'Reversal')}
    _audit(db, current_user, "sbi_run_p02", {"recon_date": recon_date, **summary})
    return {"recon_date": recon_date, "total": len(results), "summary": summary}


# ── P03: Run CSP-Transaction-Bank Reconciliation ──────────────────────────────

@router.post("/run/p03")
def run_p03(
    recon_date: str,
    upload_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("run_recon")),
):
    """
    P03 — CSP-Transaction-Bank Reconciliation.
    Money OUT (Transaction Report debits to CSP) ↔ Money IN (Bank credits from CSP).
    Match key: CSP Code + Amount.
    4-priority date logic (SOP Section 4):
      P1: same-day | P2: D+1 bank→txn | P3: D+1 txn→bank | P4: D-1 txn→bank
    One-to-one matching (no duplicate assignment).
    """
    today = upload_date or str(datetime.date.today())
    db.query(SBIP03Result).filter(SBIP03Result.recon_date == recon_date).delete()
    db.commit()

    # Load CSP Master for mode lookup
    csp_modes = {}
    for r in db.query(SBICSPMaster).filter(SBICSPMaster.upload_date == today).all():
        if r.csp_code not in csp_modes:
            csp_modes[r.csp_code] = r.mode

    # Load Transaction Report (money paid OUT to CSP = debits from settlement account)
    txn_rows = []
    for r in db.query(SBITxnReport).filter(SBITxnReport.upload_date == today).all():
        if r.status and r.status.lower() == 'success' and r.amount and r.amount > 0:
            txn_rows.append(r)

    # Load Bank Statement credits (money received FROM CSP)
    bank_credits = []
    for r in db.query(SBIBankTransaction).filter(
        SBIBankTransaction.upload_date == today,
        SBIBankTransaction.credit > 0,
        SBIBankTransaction.is_settlement == False,
    ).all():
        bank_credits.append(r)

    # Build bank lookup: (ko_id, amount, date) → bank row
    # Date offsets checked: 0, +1, -1
    bank_by_ko_amt = {}
    for b in bank_credits:
        key = (b.ko_id, round(b.credit, 2))
        bank_by_ko_amt.setdefault(key, []).append(b)

    results = []
    used_bank_ids = set()

    def _find_bank(ko_id, amount, txn_date_str, max_shift=1):
        """Find best matching bank credit by KO ID + Amount with date shifting."""
        key = (ko_id, round(amount, 2))
        candidates = bank_by_ko_amt.get(key, [])
        # Sort candidates by date proximity
        best = None
        best_shift = 999
        for b in candidates:
            if b.id in used_bank_ids: continue
            try:
                from datetime import date
                t = date.fromisoformat(txn_date_str) if txn_date_str else None
                bd = date.fromisoformat(b.txn_date) if b.txn_date else None
                if t and bd:
                    shift = (bd - t).days
                    if abs(shift) <= max_shift and abs(shift) < abs(best_shift):
                        best = b
                        best_shift = shift
            except Exception:
                if best is None:
                    best = b
                    best_shift = 0
        return best, best_shift if best else 999

    # Match each transaction report row against bank credits
    for txn in txn_rows:
        ko = txn.ko_id
        amt = txn.amount or 0
        mode = csp_modes.get(ko, 'Unknown')

        bank_match, shift = _find_bank(ko, amt, txn.txn_date, max_shift=2)

        if bank_match:
            used_bank_ids.add(bank_match.id)
            if shift == 0: priority = 1
            elif shift == 1: priority = 2   # D+1 bank
            elif shift == -1: priority = 4   # D-1 bank
            else: priority = 3

            result = SBIP03Result(
                id              = generate_id(),
                recon_date      = recon_date,
                csp_code        = ko,
                mode            = mode,
                ref_number      = txn.reference_number,
                txn_amount      = amt,
                txn_date        = txn.txn_date,
                bank_credit_date= bank_match.txn_date,
                bank_amount     = bank_match.credit,
                match_status    = 'Matched',
                date_shift      = shift,
                match_priority  = priority,
                notes           = f"Shift {shift:+d}d" if shift != 0 else '',
            )
        else:
            result = SBIP03Result(
                id              = generate_id(),
                recon_date      = recon_date,
                csp_code        = ko,
                mode            = mode,
                ref_number      = txn.reference_number,
                txn_amount      = amt,
                txn_date        = txn.txn_date,
                bank_credit_date= None,
                bank_amount     = None,
                match_status    = 'Unmatched_TxnReport',
                date_shift      = None,
                match_priority  = None,
                notes           = 'No matching bank credit found (checked ±2 days)',
            )

        db.add(result)
        results.append(result)

    # Unmatched bank credits
    for b in bank_credits:
        if b.id not in used_bank_ids:
            mode = csp_modes.get(b.ko_id, 'Unknown')
            result = SBIP03Result(
                id              = generate_id(),
                recon_date      = recon_date,
                csp_code        = b.ko_id,
                mode            = mode,
                ref_number      = b.ref_number,
                txn_amount      = None,
                txn_date        = None,
                bank_credit_date= b.txn_date,
                bank_amount     = b.credit,
                match_status    = 'Unmatched_Bank',
                date_shift      = None,
                match_priority  = None,
                notes           = 'Bank credit with no matching transaction report entry',
            )
            db.add(result)
            results.append(result)

    db.commit()
    summary = {s: sum(1 for r in results if r.match_status == s)
               for s in ('Matched', 'Unmatched_TxnReport', 'Unmatched_Bank')}
    _audit(db, current_user, "sbi_run_p03", {"recon_date": recon_date, **summary})
    return {"recon_date": recon_date, "total": len(results), "summary": summary}


# ── P04: Run Wallet Balance Reconciliation ────────────────────────────────────

@router.post("/run/p04")
def run_p04(
    recon_date: str,
    upload_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("run_recon")),
):
    """
    P04 — CSP Wallet Balance Reconciliation.
    For each KO in the Limit Update Failure Report:
      - Find their Closing Balance in KO Cash Holding Report
      - Determine whether wallet needs DEPOSIT or WITHDRAWAL correction
    Action is flagged; team marks done after performing action in SBI portal.
    """
    today = upload_date or str(datetime.date.today())
    db.query(SBIP04Result).filter(SBIP04Result.recon_date == recon_date).delete()
    db.commit()

    # Load KO Cash Holding closing balances
    cash_holding = {}
    for r in db.query(SBIKOCashHolding).filter(SBIKOCashHolding.upload_date == today).all():
        cash_holding[r.ko_id] = r.closing_balance or 0

    # Load Limit Failures
    failures = db.query(SBILimitFailure).filter(
        SBILimitFailure.upload_date == today
    ).all()

    results = []
    for f in failures:
        closing = cash_holding.get(f.csp_code, 0)
        failed_amt = f.amount or 0  # negative = withdrawal attempt, positive = deposit attempt

        # Expected balance after the failed operation would have succeeded
        expected = closing + failed_amt   # if withdrawal failed, wallet has MORE than expected

        diff = closing - expected         # actual vs expected
        abs_diff = abs(diff)

        if abs_diff < 0.01:
            action = 'NONE'
            action_amt = 0.0
        elif diff > 0:
            # Wallet has MORE than expected → withdrawal needed
            action = 'WITHDRAWAL'
            action_amt = diff
        else:
            # Wallet has LESS than expected → deposit needed
            action = 'DEPOSIT'
            action_amt = abs_diff

        result = SBIP04Result(
            id              = generate_id(),
            recon_date      = recon_date,
            csp_code        = f.csp_code,
            failed_amount   = failed_amt,
            closing_balance = closing,
            expected_balance= expected,
            difference      = round(diff, 2),
            action_required = action,
            action_amount   = round(action_amt, 2),
            action_done     = False,
            notes           = f"Limit update failed on {f.txn_date}: {failed_amt:,.0f}. "
                              f"Current balance: {closing:,.0f}, Expected: {expected:,.0f}",
        )
        db.add(result)
        results.append(result)

    db.commit()
    summary = {a: sum(1 for r in results if r.action_required == a)
               for a in ('DEPOSIT', 'WITHDRAWAL', 'NONE')}
    _audit(db, current_user, "sbi_run_p04", {"recon_date": recon_date, **summary})
    return {"recon_date": recon_date, "total": len(results), "summary": summary}


# ── P04: Mark action done ─────────────────────────────────────────────────────

@router.patch("/p04/{result_id}/done")
def mark_p04_done(
    result_id: str,
    done: bool = True,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """Mark a P04 wallet adjustment as completed (after team acts in SBI portal)."""
    r = db.query(SBIP04Result).filter(SBIP04Result.id == result_id).first()
    if not r: raise HTTPException(status_code=404, detail="Result not found")
    r.action_done = done
    db.commit()
    return {"message": "Updated"}


# ── Query Endpoints ───────────────────────────────────────────────────────────

@router.get("/p01/results")
def get_p01_results(
    recon_date: Optional[str] = None,
    status: Optional[str] = None,
    ko_id: Optional[str] = None,
    txn_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    q = db.query(SBIP01Result)
    if recon_date: q = q.filter(SBIP01Result.recon_date == recon_date)
    if status: q = q.filter(SBIP01Result.status == status)
    if ko_id:  q = q.filter(SBIP01Result.ko_id.like(f"%{ko_id}%"))
    if txn_date:  # match the bank statement date OR the wallet deduction date (D±1)
        q = q.filter((SBIP01Result.bank_txn_date == txn_date) | (SBIP01Result.deduct_date == txn_date))
    rows = q.order_by(SBIP01Result.ko_id).all()
    grand = {"wallet_withdrawn": 0.0, "bank_settled": 0.0, "difference": 0.0}
    data = []
    for r in rows:
        grand["wallet_withdrawn"] += r.wallet_withdrawn or 0
        grand["bank_settled"]     += r.bank_settled or 0
        grand["difference"]       += r.difference or 0
        data.append({k: getattr(r, k) for k in ('id','ko_id','wallet_withdrawn','bank_settled','difference','status','deduct_date','bank_txn_date','notes','recon_date')})
    data = _apply_src_assignments(db, "p01", recon_date, data)
    return {"rows": data, "grand": {k: round(v, 2) for k, v in grand.items()}, "count": len(data)}


@router.get("/p01/lines")
def get_p01_lines(
    recon_date: str,
    upload_date: Optional[str] = None,
    ko_id: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    P01 line-level (bank <-> data) drill — the P02-style view for P01: the individual
    EKOSETTLEMENT bank lines and KO-withdrawal lines that make up each KO's P01 totals.
    Source rows are keyed by upload_date (defaults to recon_date — the common same-day
    case; override if the run used a different upload_date; see behaviour-contract #17
    on the SBI upload_date coupling).
    """
    ud = upload_date or recon_date
    p01q = db.query(SBIP01Result).filter(SBIP01Result.recon_date == recon_date)
    if ko_id:  p01q = p01q.filter(SBIP01Result.ko_id.like(f"%{ko_id}%"))
    if status: p01q = p01q.filter(SBIP01Result.status == status)
    p01 = {r.ko_id: r for r in p01q.all()}
    wanted = set(p01.keys())

    groups = {}
    def _g(ko):
        if ko not in groups:
            r = p01.get(ko)
            groups[ko] = {
                "ko_id": ko,
                "status": r.status if r else None,
                "wallet_withdrawn": r.wallet_withdrawn if r else 0.0,
                "bank_settled": r.bank_settled if r else 0.0,
                "difference": r.difference if r else 0.0,
                "deduct_date": r.deduct_date if r else "",
                "bank_txn_date": r.bank_txn_date if r else "",
                "withdrawals": [], "settlements": [],
            }
        return groups[ko]

    for w in db.query(SBIKOLimits).filter(
            SBIKOLimits.txn_type == "KO Withdrawal", SBIKOLimits.upload_date == ud).all():
        if wanted and w.ko_id not in wanted:
            continue
        _g(w.ko_id)["withdrawals"].append({
            "amount": w.amount, "txn_date": w.txn_date, "datetime": w.txn_datetime,
            "configured_by": w.limit_configured_by})
    for s in db.query(SBIBankTransaction).filter(
            SBIBankTransaction.is_settlement == True, SBIBankTransaction.upload_date == ud).all():
        if not s.ko_id or (wanted and s.ko_id not in wanted):
            continue
        _g(s.ko_id)["settlements"].append({
            "amount": s.debit, "txn_date": s.txn_date, "deduct_date": s.deduct_date,
            "description": s.description, "ref": s.ref_number})

    for ko in wanted:               # KOs with a P01 row but no source lines still show
        _g(ko)

    rows = sorted(groups.values(), key=lambda x: x["ko_id"] or "")
    rows = _apply_src_assignments(db, "p01", recon_date, rows)   # KO-level SRC overlay
    return {"recon_date": recon_date, "upload_date": ud, "kos": rows, "count": len(rows)}


@router.get("/p02/results")
def get_p02_results(
    recon_date: Optional[str] = None,
    match_status: Optional[str] = None,
    ko_id: Optional[str] = None,
    reference: Optional[str] = None,
    bank_type: Optional[str] = None,
    page: int = 1,
    page_size: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # Filters that narrow which rows are shown (applied to the row list AND the
    # summary so counts match the filtered view). match_status is the summary's
    # grouping key, so it's NOT applied to the summary.
    def _common(query):
        if recon_date: query = query.filter(SBIP02Result.recon_date == recon_date)
        if ko_id:      query = query.filter(SBIP02Result.ko_id.like(f"%{ko_id}%"))
        if reference:  query = query.filter(SBIP02Result.reference_number.like(f"%{reference}%"))
        if bank_type:  query = query.filter(SBIP02Result.bank_type == bank_type)
        return query

    q = _common(db.query(SBIP02Result))
    if match_status: q = q.filter(SBIP02Result.match_status == match_status)
    total = q.count()
    # Summary from the filtered set (excluding the match_status filter)
    from sqlalchemy import func as sqlfunc
    summary_q = _common(db.query(SBIP02Result.match_status, sqlfunc.count(SBIP02Result.id)))
    summary = {s: c for s, c in summary_q.group_by(SBIP02Result.match_status).all()}
    rows = q.order_by(SBIP02Result.reference_number).offset((page-1)*page_size).limit(page_size).all()
    # Bank-statement narration (read-only) for this page's rows, via the
    # SBIP02Result → SBIBankTransaction FK. One batched lookup, no N+1.
    # Read-only enrichment for the paired view (purely additive — no match-logic change):
    # the bank line's date + narration (via bank_txn_id) and the matched transaction
    # report's date/journal/status/ref (via txn_report_id). Two batched lookups, no N+1.
    _bt_ids = [r.bank_txn_id for r in rows if r.bank_txn_id]
    _bank_map = {}
    if _bt_ids:
        for _bid, _bdesc, _bdate in db.query(
            SBIBankTransaction.id, SBIBankTransaction.description, SBIBankTransaction.txn_date
        ).filter(SBIBankTransaction.id.in_(_bt_ids)).all():
            _bank_map[_bid] = (_bdesc, _bdate)
    _tr_ids = [r.txn_report_id for r in rows if r.txn_report_id]
    _rep_map = {}
    if _tr_ids:
        for _rid, _rdate, _rjrnl, _rstat, _rref in db.query(
            SBITxnReport.id, SBITxnReport.txn_date, SBITxnReport.journal_number,
            SBITxnReport.status, SBITxnReport.reference_number
        ).filter(SBITxnReport.id.in_(_tr_ids)).all():
            _rep_map[_rid] = (_rdate, _rjrnl, _rstat, _rref)
    data = []
    for r in rows:
        _b = _bank_map.get(r.bank_txn_id) or ('', '')
        _rp = _rep_map.get(r.txn_report_id) or ('', '', '', '')
        data.append({
            **{k: getattr(r, k) for k in ('id','reference_number','ko_id','bank_amount','bank_type','report_amount','report_txn_type','match_status','reversal_type','success_status','notes','recon_date')},
            'bank_description': _b[0] or '',
            'bank_txn_date':   _b[1] or '',
            'report_date':     _rp[0] or '',
            'report_journal':  _rp[1] or '',
            'report_status':   _rp[2] or '',
            'report_ref':      _rp[3] or '',
        })
    data = _apply_manual_matches(db, "p02", recon_date, data)
    data = _apply_src_assignments(db, "p02", recon_date, data)
    _mmq = db.query(SBIManualMatch).filter(SBIManualMatch.process == "p02")
    if recon_date: _mmq = _mmq.filter(SBIManualMatch.recon_date == recon_date)
    return {"rows": data, "summary": summary, "manual_matched_count": _mmq.count(),
            "total": total, "page": page, "page_size": page_size}


@router.get("/p03/results")
def get_p03_results(
    recon_date: Optional[str] = None,
    match_status: Optional[str] = None,
    csp_code: Optional[str] = None,
    mode: Optional[str] = None,
    txn_date: Optional[str] = None,
    page: int = 1,
    page_size: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    def _common(query):
        if recon_date: query = query.filter(SBIP03Result.recon_date == recon_date)
        if csp_code:   query = query.filter(SBIP03Result.csp_code.like(f"%{csp_code}%"))
        if mode:       query = query.filter(SBIP03Result.mode == mode)
        if txn_date:   # the transaction date OR the bank credit date
            query = query.filter((SBIP03Result.txn_date == txn_date) | (SBIP03Result.bank_credit_date == txn_date))
        return query

    q = _common(db.query(SBIP03Result))
    if match_status: q = q.filter(SBIP03Result.match_status == match_status)
    total = q.count()
    from sqlalchemy import func as sqlfunc
    summary_q = _common(db.query(SBIP03Result.match_status, sqlfunc.count(SBIP03Result.id)))
    summary = {s: c for s, c in summary_q.group_by(SBIP03Result.match_status).all()}
    rows = q.order_by(SBIP03Result.csp_code).offset((page-1)*page_size).limit(page_size).all()
    data = [{k: getattr(r, k) for k in ('id','csp_code','mode','ref_number','txn_amount','txn_date','bank_credit_date','bank_amount','match_status','date_shift','match_priority','notes','recon_date')} for r in rows]
    data = _apply_manual_matches(db, "p03", recon_date, data)
    data = _apply_src_assignments(db, "p03", recon_date, data)
    _mmq = db.query(SBIManualMatch).filter(SBIManualMatch.process == "p03")
    if recon_date: _mmq = _mmq.filter(SBIManualMatch.recon_date == recon_date)
    return {"rows": data, "summary": summary, "manual_matched_count": _mmq.count(),
            "total": total, "page": page, "page_size": page_size}


@router.get("/p04/results")
def get_p04_results(
    recon_date: Optional[str] = None,
    action_required: Optional[str] = None,
    csp_code: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    q = db.query(SBIP04Result)
    if recon_date:      q = q.filter(SBIP04Result.recon_date == recon_date)
    if action_required: q = q.filter(SBIP04Result.action_required == action_required)
    if csp_code:        q = q.filter(SBIP04Result.csp_code.like(f"%{csp_code}%"))
    rows = q.order_by(SBIP04Result.csp_code).all()
    summary = {}
    for r in rows:
        summary[r.action_required] = summary.get(r.action_required, 0) + 1
    data = [{k: getattr(r, k) for k in ('id','csp_code','failed_amount','closing_balance','expected_balance','difference','action_required','action_amount','action_done','notes','recon_date')} for r in rows]
    data = _apply_src_assignments(db, "p04", recon_date, data)
    return {"rows": data, "summary": summary, "count": len(data)}


# ── Unified ledger — every bank & data entry + how each reconciled (P01/P02/P03) ──

_P01_UNIFIED_STATUS = {"CREDITED": "Matched", "PENDING": "Pending",
                       "PARTIAL": "Partial", "EXCESS": "Excess"}

def _r(n) -> str:
    try:
        return f"₹{float(n or 0):,.0f}"
    except (TypeError, ValueError):
        return "₹0"


@router.get("/unified")
def get_unified(
    recon_date: str,
    upload_date: Optional[str] = None,
    side: Optional[str] = None,        # bank | data
    status: Optional[str] = None,      # Matched | Unmatched | Reversal | ...
    process: Optional[str] = None,     # p01 | p02 | p03
    search: Optional[str] = None,      # ref / KO / CSP
    page: int = 1,
    page_size: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Unified SBI ledger: EVERY source entry — bank statement lines AND data lines
    (transaction reports, KO withdrawals) — each tagged with its reconciliation
    status, which process (P01/P02/P03) reconciled it, and its counterpart. Built
    read-only from the P0x result tables; SRC + manual-match act on the mapped
    result row via the existing endpoints (result_id + result_process on each row).
    Scoped to one date (sources by upload_date≈recon_date; behaviour-contract #17).
    P03 has no source FK, so P03 links are best-effort on CSP+amount.
    """
    ud = upload_date or recon_date

    p01_by_ko = {x.ko_id: x for x in db.query(SBIP01Result).filter(SBIP01Result.recon_date == recon_date).all()}
    p02_by_bank, p02_by_report = {}, {}
    for x in db.query(SBIP02Result).filter(SBIP02Result.recon_date == recon_date).all():
        if x.bank_txn_id:   p02_by_bank[x.bank_txn_id] = x
        if x.txn_report_id: p02_by_report[x.txn_report_id] = x
    # P03 has no source FK — link best-effort by CSP + amount. p03_matched (Matched only)
    # drives the "also in P03" overlay on bank credits; p03_by_txn (all statuses, money-out
    # side, keyed incl. ref) maps transaction-report lines to their P03 row for SRC/actions.
    p03_matched, p03_by_txn = {}, {}
    for x in db.query(SBIP03Result).filter(SBIP03Result.recon_date == recon_date).all():
        if not x.csp_code:
            continue
        if x.match_status == "Matched":
            a = x.txn_amount if x.txn_amount is not None else x.bank_amount
            if a is not None:
                p03_matched[(x.csp_code, round(float(a), 2))] = x
        if x.txn_amount is not None:
            p03_by_txn[(x.csp_code, round(float(x.txn_amount), 2), x.ref_number or "")] = x

    entries = []

    def _new(sidev, src, row_id, ref, ko, amt, date, drcr, narration):
        return {"side": sidev, "source": src, "id": row_id, "ref": ref or "", "ko_csp": ko or "",
                "amount": amt, "date": date or "", "drcr": drcr, "narration": narration or "",
                "status": "Not reconciled", "process": "", "counterpart": None, "also_p03": False,
                "result_id": None, "result_process": None, "src_code": None, "src_note": None,
                "_result": None}

    # ---- bank side ----
    for b in db.query(SBIBankTransaction).filter(SBIBankTransaction.upload_date == ud).all():
        drcr = "DR" if (b.debit or 0) > 0 else ("CR" if (b.credit or 0) > 0 else "")
        amt = (b.debit or 0) if (b.debit or 0) > 0 else (b.credit or 0)
        if b.is_settlement:
            e = _new("bank", "Bank Settlement", b.id, b.ref_number, b.ko_id, amt, b.txn_date, drcr, b.description)
            r = p01_by_ko.get(b.ko_id)
            if r:
                e.update(status=_P01_UNIFIED_STATUS.get(r.status, r.status), process="P01",
                         counterpart=f"Wallet out {_r(r.wallet_withdrawn)}", result_id=r.id,
                         result_process="p01", _result=r)
        else:
            e = _new("bank", "Bank Statement", b.id, b.ref_number, b.ko_id, amt, b.txn_date, drcr, b.description)
            r = p02_by_bank.get(b.id)
            if r:
                e.update(status=r.match_status, process="P02", result_id=r.id, result_process="p02", _result=r,
                         counterpart=(f"Report {_r(r.report_amount)} · {r.report_txn_type}" if r.report_amount is not None else None))
            if drcr == "CR" and (b.ko_id, round(float(amt), 2)) in p03_matched:
                e["also_p03"] = True
                if e["status"] in ("Unmatched", "Not reconciled"):
                    pr = p03_matched[(b.ko_id, round(float(amt), 2))]
                    e.update(status="Matched", process="P03", result_id=pr.id, result_process="p03", _result=pr,
                             counterpart=f"Txn out {_r(pr.txn_amount)}")
        entries.append(e)

    # ---- data side: transaction reports ----
    for t in db.query(SBITxnReport).filter(SBITxnReport.upload_date == ud).all():
        amt = t.amount or 0
        e = _new("data", "Txn Report", t.id, t.reference_number, t.ko_id, amt, t.txn_date, "", t.txn_type)
        e["status"] = "Unmatched"
        r = p02_by_report.get(t.id)
        if r:
            e.update(status=r.match_status, process="P02", result_id=r.id, result_process="p02", _result=r,
                     counterpart=f"Bank {r.bank_type} {_r(r.bank_amount)}")
        else:
            pr = p03_by_txn.get((t.ko_id, round(float(amt), 2), t.reference_number or ""))
            if pr:
                st = {"Unmatched_TxnReport": "Unmatched", "Unmatched_Bank": "Unmatched"}.get(pr.match_status, pr.match_status)
                e.update(status=st, process="P03", also_p03=(pr.match_status == "Matched"),
                         result_id=pr.id, result_process="p03", _result=pr,
                         counterpart=(f"Bank credit {_r(pr.bank_amount)}" if pr.bank_amount is not None else None))
        entries.append(e)

    # ---- data side: KO withdrawals ----
    for w in db.query(SBIKOLimits).filter(SBIKOLimits.txn_type == "KO Withdrawal",
                                          SBIKOLimits.upload_date == ud).all():
        e = _new("data", "KO Withdrawal", w.id, "", w.ko_id, w.amount or 0, w.txn_date, "", "KO Withdrawal")
        r = p01_by_ko.get(w.ko_id)
        if r:
            e.update(status=_P01_UNIFIED_STATUS.get(r.status, r.status), process="P01",
                     counterpart=f"Bank settled {_r(r.bank_settled)}", result_id=r.id,
                     result_process="p01", _result=r)
        entries.append(e)

    # ---- filters ----
    if side:    entries = [e for e in entries if e["side"] == side]
    if process: entries = [e for e in entries if (e["process"] or "").lower() == process.lower()]
    if status:  entries = [e for e in entries if (e["status"] or "").lower() == status.lower()]
    if search:
        s = search.lower()
        entries = [e for e in entries if s in (e["ref"] or "").lower() or s in (e["ko_csp"] or "").lower()]

    from collections import Counter
    status_counts = dict(Counter(e["status"] for e in entries))
    side_counts   = dict(Counter(e["side"] for e in entries))
    total = len(entries)

    entries.sort(key=lambda e: (e["ko_csp"] or "", e["ref"] or "", e["side"]))
    page_rows = entries[(page - 1) * page_size: page * page_size]

    # SRC-tag overlay for the page rows, from the mapped result's stable key
    sa = {(a.process, a.match_key): a for a in db.query(SBISrcAssignment).filter(
          SBISrcAssignment.recon_date == recon_date).all()}
    for e in page_rows:
        r = e.pop("_result", None)
        if r is not None and e["result_process"]:
            rd = {c.name: getattr(r, c.name) for c in r.__table__.columns}
            key = _src_key(e["result_process"], rd)
            a = sa.get((e["result_process"], key)) if key else None
            if a:
                e["src_code"], e["src_note"] = a.src_code, a.src_note
    for e in entries:                # drop the non-serialisable ref on any non-page rows
        e.pop("_result", None)

    return {"rows": page_rows, "total": total, "page": page, "page_size": page_size,
            "status_counts": status_counts, "side_counts": side_counts,
            "recon_date": recon_date, "upload_date": ud}


# ── Export ─────────────────────────────────────────────────────────────────────
# Each SBI process reconciles in its OWN result table (NOT the core `transactions`
# ledger), so the generic /api/reports exports return blank for partner=kiosk. These
# export the real recon results, with the operator-facing names the team uses.
#   p01 = "Bank vs Settlement"   p02 = "Bank vs Transaction"
#   p03 = "CSP-Txn-Bank"         p04 = "Wallet Balance"
_SBI_EXPORTS = {
    "p01": ("Bank vs Settlement", SBIP01Result, "ko_id",
            ['recon_date', 'ko_id', 'wallet_withdrawn', 'bank_settled', 'difference',
             'status', 'deduct_date', 'bank_txn_date', 'notes']),
    "p02": ("Bank vs Transaction", SBIP02Result, "reference_number",
            ['recon_date', 'reference_number', 'ko_id', 'bank_amount', 'bank_type',
             'report_amount', 'report_txn_type', 'match_status', 'reversal_type',
             'success_status', 'notes']),
    "p03": ("CSP-Txn-Bank", SBIP03Result, "csp_code",
            ['recon_date', 'csp_code', 'mode', 'ref_number', 'txn_amount', 'txn_date',
             'bank_credit_date', 'bank_amount', 'match_status', 'date_shift',
             'match_priority', 'notes']),
    "p04": ("Wallet Balance", SBIP04Result, "csp_code",
            ['recon_date', 'csp_code', 'failed_amount', 'closing_balance',
             'expected_balance', 'difference', 'action_required', 'action_amount',
             'action_done', 'notes']),
}


def _build_sbi_export(db, which, recon_date=None, match_status=None,
                      date_from=None, date_to=None) -> io.BytesIO:
    """Build a styled multi-sheet SBI recon workbook; one sheet per process in `which`.
    Always writes a header row (a valid file even with zero data rows).
    recon_date = single date (unchanged behaviour); date_from/date_to = range."""
    from openpyxl.styles import PatternFill, Font as XFont
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for p in which:
            sheet, model, order_col, cols = _SBI_EXPORTS[p]
            q = db.query(model)
            if recon_date:
                q = q.filter(model.recon_date == recon_date)
            else:
                if date_from: q = q.filter(model.recon_date >= date_from)
                if date_to:   q = q.filter(model.recon_date <= date_to)
            # match_status only applies to the processes that have that column (p02/p03)
            if match_status and hasattr(model, "match_status"):
                q = q.filter(model.match_status == match_status)
            rows = q.order_by(getattr(model, order_col)).all()
            recs = [{c: getattr(r, c) for c in cols} for r in rows]
            if p in ("p02", "p03"):
                recs = _apply_manual_matches(db, p, recon_date, recs)
            df = pd.DataFrame(recs, columns=cols)
            df.to_excel(writer, sheet_name=sheet[:31], index=False)
            ws = writer.sheets[sheet[:31]]
            fill = PatternFill("solid", fgColor="094053")
            for cell in ws[1]:
                cell.fill = fill
                cell.font = XFont(color="FFFFFF", bold=True)
            for i, c in enumerate(cols, 1):
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(12, min(42, len(str(c)) + 4))
    output.seek(0)
    return output


@router.get("/export")
def export_sbi(
    process: str = Query("all", description="p01 | p02 | p03 | p04 | all"),
    recon_date: Optional[str] = None,
    match_status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Export SBI Kiosk recon results to a styled Excel workbook.

    `process=all` → one workbook with one sheet per process (Bank vs Settlement,
    Bank vs Transaction, CSP-Txn-Bank, Wallet Balance). A specific process → a
    single-sheet file. Always returns a valid file (header row even when empty) —
    this replaces the old core-ledger export that came back blank because SBI
    reconciles in its own tables.
    """
    process = (process or "all").lower()
    which = list(_SBI_EXPORTS) if process == "all" else [process]
    if any(p not in _SBI_EXPORTS for p in which):
        raise HTTPException(status_code=400, detail="process must be one of p01, p02, p03, p04, all")
    # Never hand back a silently-blank file: if a recon_date was requested but has no
    # rows for any requested process (wrong date, or recon not run that day), fall back
    # to the latest date that DOES have data. `used_date` is echoed in a header so the
    # UI can tell the operator which date was actually exported (or that none exists).
    used_date = recon_date
    if recon_date:
        has_here = any(
            db.query(_SBI_EXPORTS[p][1].id).filter(_SBI_EXPORTS[p][1].recon_date == recon_date).first()
            for p in which
        )
        if not has_here:
            latest = None
            for p in which:
                m = _SBI_EXPORTS[p][1]
                d = db.query(func.max(m.recon_date)).filter(m.recon_date.isnot(None)).scalar()
                if d and (latest is None or d > latest):
                    latest = d
            used_date = latest  # None only when the process has no data on any date
    output = _build_sbi_export(db, which, used_date, match_status, date_from, date_to)
    _tag = used_date or (f"{date_from or 'start'}_to_{date_to or 'end'}"
                         if (date_from or date_to) else "all")
    fname = f"sbi_{process}_{_tag}.xlsx"
    # Header semantics the UI relies on: the actual date exported, or the range/"all"
    # tag when exporting a span WITH data, or "none" only when there is truly no data.
    if used_date:
        _hdr = used_date
    else:
        _has = any(_rng(db.query(_SBI_EXPORTS[p][1].id), _SBI_EXPORTS[p][1],
                        None, date_from, date_to).first() for p in which)
        _hdr = _tag if _has else "none"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Recon-Date": str(_hdr),
            "Access-Control-Expose-Headers": "X-Recon-Date",
        },
    )


# ── Report library ─────────────────────────────────────────────────────────────
# The full set of SBI report options beyond the raw per-process exports. All are
# READ-ONLY over the result/source/overlay tables — no matching logic involved.
# Range filters compare the zero-padded recon_date strings lexicographically
# (app-wide convention). Single-date reports fall back to the latest date with
# data and echo it in X-Recon-Date (same pattern as /export).

def _style_report_ws(ws, ncols: int):
    from openpyxl.styles import PatternFill, Font as XFont
    fill = PatternFill("solid", fgColor="094053")
    for cell in ws[1]:
        cell.fill = fill
        cell.font = XFont(color="FFFFFF", bold=True)
    for i in range(1, ncols + 1):
        c = ws.cell(row=1, column=i)
        ws.column_dimensions[c.column_letter].width = max(12, min(46, len(str(c.value or "")) + 4))


def _sheet(writer, name: str, rows: list, cols: list):
    """Write one styled sheet; always valid (header row even with zero rows)."""
    df = pd.DataFrame(rows, columns=cols)
    df.to_excel(writer, sheet_name=name[:31], index=False)
    _style_report_ws(writer.sheets[name[:31]], len(cols))


def _rng(q, model, recon_date, date_from, date_to):
    if recon_date:
        return q.filter(model.recon_date == recon_date)
    if date_from:
        q = q.filter(model.recon_date >= date_from)
    if date_to:
        q = q.filter(model.recon_date <= date_to)
    return q


def _latest_sbi_date(db, models=None) -> Optional[str]:
    latest = None
    for m in (models or [SBIP01Result, SBIP02Result, SBIP03Result, SBIP04Result]):
        d = db.query(func.max(m.recon_date)).scalar()
        if d and (latest is None or d > latest):
            latest = d
    return latest


def _bank_desc_map(db, ids: list) -> dict:
    """id → (description, txn_date) for bank rows, chunked (IN-list safe on MySQL)."""
    out = {}
    ids = [i for i in ids if i]
    for i in range(0, len(ids), 900):
        for _id, _d, _dt in db.query(SBIBankTransaction.id, SBIBankTransaction.description,
                                     SBIBankTransaction.txn_date
                                     ).filter(SBIBankTransaction.id.in_(ids[i:i + 900])).all():
            out[_id] = (_d or '', _dt or '')
    return out


def _load_process_recs(db, p: str, recon_date, date_from=None, date_to=None,
                       with_bank_desc: bool = False) -> list:
    """Load one process's result rows as dicts (export columns) with the manual-match
    and SRC overlays applied — the same read-time view the results endpoints serve."""
    sheet, model, order_col, cols = _SBI_EXPORTS[p]
    q = _rng(db.query(model), model, recon_date, date_from, date_to)
    rows = q.order_by(getattr(model, order_col)).all()
    recs = [{c: getattr(r, c) for c in cols} for r in rows]
    if p == "p02" and with_bank_desc and rows:
        bm = _bank_desc_map(db, [r.bank_txn_id for r in rows])
        for rec, r in zip(recs, rows):
            d = bm.get(r.bank_txn_id) or ('', '')
            rec["bank_description"], rec["bank_txn_date"] = d[0], d[1]
    if p in ("p02", "p03"):
        recs = _apply_manual_matches(db, p, recon_date, recs)
    recs = _apply_src_assignments(db, p, recon_date, recs)
    return recs


def _proc_overview(p: str, recs: list) -> dict:
    """Per-process roll-up row for the Overview sheet."""
    total = len(recs)
    if p == "p01":
        matched = sum(1 for r in recs if r.get("status") == "CREDITED")
        amt_all = sum(r.get("wallet_withdrawn") or 0 for r in recs)
        amt_ok = sum(r.get("wallet_withdrawn") or 0 for r in recs if r.get("status") == "CREDITED")
        note = "matched = CREDITED; amounts = wallet withdrawals"
    elif p == "p04":
        matched = sum(1 for r in recs if r.get("action_required") == "NONE")
        amt_all = sum(abs(r.get("action_amount") or 0) for r in recs)
        amt_ok = 0.0
        note = "matched = no action needed; amount = pending corrections"
    else:
        matched = sum(1 for r in recs if r.get("match_status") in ("Matched", "Manual_Matched"))
        amt_field = "bank_amount" if p == "p02" else "txn_amount"
        amt_all = sum((r.get(amt_field) or r.get("bank_amount") or 0) for r in recs)
        amt_ok = sum((r.get(amt_field) or r.get("bank_amount") or 0) for r in recs
                     if r.get("match_status") in ("Matched", "Manual_Matched"))
        note = "matched incl. manual matches"
    return {"process": p.upper(), "sheet": _SBI_EXPORTS[p][0], "total": total,
            "matched": matched, "open": total - matched,
            "match_rate_pct": round(matched / total * 100, 1) if total else None,
            "amount_total": round(amt_all, 2), "amount_matched": round(amt_ok, 2),
            "amount_open": round(amt_all - amt_ok, 2), "notes": note}


_P02_XCOLS = ['recon_date', 'reference_number', 'ko_id', 'bank_amount', 'bank_type',
              'report_amount', 'report_txn_type', 'match_status', 'reversal_type',
              'success_status', 'bank_txn_date', 'bank_description', 'src_code', 'src_note', 'notes']
_P03_XCOLS = ['recon_date', 'csp_code', 'mode', 'ref_number', 'txn_amount', 'txn_date',
              'bank_credit_date', 'bank_amount', 'match_status', 'date_shift',
              'match_priority', 'src_code', 'src_note', 'notes']
_P01_XCOLS = ['recon_date', 'ko_id', 'wallet_withdrawn', 'bank_settled', 'difference',
              'status', 'deduct_date', 'bank_txn_date', 'src_code', 'src_note', 'notes']
_P04_XCOLS = ['recon_date', 'csp_code', 'failed_amount', 'closing_balance', 'expected_balance',
              'difference', 'action_required', 'action_amount', 'action_done',
              'src_code', 'src_note', 'notes']

_SBI_REPORTS = [
    {"id": "daily_pack",  "label": "Daily Recon Pack",          "scope": "date",
     "desc": "One workbook for the day: overview, all four processes, exceptions, reversals, SRC & manual logs."},
    {"id": "exceptions",  "label": "Exceptions / Work List",    "scope": "range",
     "desc": "Only what needs action: P01 not credited, P02/P03 unmatched or partial, P04 pending wallet actions."},
    {"id": "summary",     "label": "MIS Summary (trend)",       "scope": "range",
     "desc": "Per date × process: totals, matched, open, match rate and amounts — management trend view."},
    {"id": "ko_wise",     "label": "KO / CSP-wise Summary",     "scope": "range",
     "desc": "Per agent roll-up across P01–P03: volumes, matched vs open counts and amounts."},
    {"id": "unified",     "label": "All Entries (unified ledger)", "scope": "date",
     "desc": "Every bank and data entry with its status, which process reconciled it, and its counterpart."},
    {"id": "p01_lines",   "label": "P01 Settlement Lines",      "scope": "date",
     "desc": "Line-level P01: each KO withdrawal and each bank settlement line behind every KO total."},
    {"id": "reversals",   "label": "Reversals Report",          "scope": "range",
     "desc": "All P02 reversal legs (same reference DR + CR) with success/fail state."},
    {"id": "p04_actions", "label": "Wallet Action Tracker (P04)", "scope": "range",
     "desc": "Deposit/withdrawal corrections with done vs pending status."},
    {"id": "src_log",     "label": "SRC Disposition Log",       "scope": "range",
     "desc": "Every SRC tag on SBI rows: code, note, who and when."},
    {"id": "manual_log",  "label": "Manual Match Log",          "scope": "range",
     "desc": "Every SBI manual match: key, counterpart, remark, who and when."},
]


@router.get("/report-options")
def sbi_report_options(current_user=Depends(get_current_user)):
    """The report library (drives the Reports UI — additions are backend-only)."""
    return {"reports": _SBI_REPORTS}


@router.get("/report")
def sbi_report(
    type: str = Query(..., description="one of the /sbi/report-options ids"),
    recon_date: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Build one report from the SBI report library as a styled Excel workbook."""
    t = (type or "").lower()
    if t not in {r["id"] for r in _SBI_REPORTS}:
        raise HTTPException(status_code=400, detail="Unknown report type — see /sbi/report-options")

    # Single-date reports: fall back to the latest date with data (echoed in header).
    used_date = recon_date
    scope = next(r["scope"] for r in _SBI_REPORTS if r["id"] == t)
    if scope == "date":
        if used_date:
            has = any(db.query(m.id).filter(m.recon_date == used_date).first()
                      for m in (SBIP01Result, SBIP02Result, SBIP03Result, SBIP04Result))
            if not has:
                used_date = _latest_sbi_date(db)
        else:
            used_date = _latest_sbi_date(db)
        rd, df_, dt_ = used_date, None, None
        tag = used_date or "none"
    else:
        rd, df_, dt_ = recon_date, date_from, date_to
        tag = rd or (f"{df_ or 'start'}_to_{dt_ or 'end'}" if (df_ or dt_) else "all")

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as w:

        if t == "daily_pack":
            recs = {p: _load_process_recs(db, p, rd, with_bank_desc=(p == "p02")) for p in _SBI_EXPORTS}
            _sheet(w, "Overview", [_proc_overview(p, recs[p]) for p in _SBI_EXPORTS],
                   ["process", "sheet", "total", "matched", "open", "match_rate_pct",
                    "amount_total", "amount_matched", "amount_open", "notes"])
            _sheet(w, "P01 Settlement", recs["p01"], _P01_XCOLS)
            _sheet(w, "P02 Bank vs Txn", recs["p02"], _P02_XCOLS)
            _sheet(w, "P03 CSP-Txn-Bank", recs["p03"], _P03_XCOLS)
            _sheet(w, "P04 Wallet", recs["p04"], _P04_XCOLS)
            _sheet(w, "Exceptions P01",
                   [r for r in recs["p01"] if r.get("status") != "CREDITED"], _P01_XCOLS)
            _sheet(w, "Exceptions P02",
                   [r for r in recs["p02"] if r.get("match_status") not in ("Matched", "Manual_Matched")], _P02_XCOLS)
            _sheet(w, "Exceptions P03",
                   [r for r in recs["p03"] if r.get("match_status") not in ("Matched", "Manual_Matched")], _P03_XCOLS)
            _sheet(w, "P04 Pending",
                   [r for r in recs["p04"] if r.get("action_required") != "NONE" and not r.get("action_done")], _P04_XCOLS)
            _sheet(w, "Reversals",
                   [r for r in recs["p02"] if r.get("match_status") == "Reversal"], _P02_XCOLS)
            srcs = db.query(SBISrcAssignment).filter(SBISrcAssignment.recon_date == rd).all() if rd else []
            _sheet(w, "SRC Log",
                   [{"recon_date": s.recon_date, "process": s.process, "match_key": s.match_key,
                     "src_code": s.src_code, "src_note": s.src_note, "by": s.created_by,
                     "at": str(s.created_at or "")} for s in srcs],
                   ["recon_date", "process", "match_key", "src_code", "src_note", "by", "at"])
            mms = db.query(SBIManualMatch).filter(SBIManualMatch.recon_date == rd).all() if rd else []
            _sheet(w, "Manual Log",
                   [{"recon_date": m.recon_date, "process": m.process, "match_key": m.match_key,
                     "counterpart_ref": m.counterpart_ref, "remark": m.remark, "by": m.created_by,
                     "at": str(m.created_at or "")} for m in mms],
                   ["recon_date", "process", "match_key", "counterpart_ref", "remark", "by", "at"])

        elif t == "exceptions":
            p01 = [r for r in _load_process_recs(db, "p01", rd, df_, dt_) if r.get("status") != "CREDITED"]
            p02 = [r for r in _load_process_recs(db, "p02", rd, df_, dt_, with_bank_desc=True)
                   if r.get("match_status") not in ("Matched", "Manual_Matched")]
            p03 = [r for r in _load_process_recs(db, "p03", rd, df_, dt_)
                   if r.get("match_status") not in ("Matched", "Manual_Matched")]
            p04 = [r for r in _load_process_recs(db, "p04", rd, df_, dt_)
                   if r.get("action_required") != "NONE" and not r.get("action_done")]
            _sheet(w, "P01 Not Credited", p01, _P01_XCOLS)
            _sheet(w, "P02 Open", p02, _P02_XCOLS)
            _sheet(w, "P03 Open", p03, _P03_XCOLS)
            _sheet(w, "P04 Pending Actions", p04, _P04_XCOLS)

        elif t == "summary":
            grid, totals = [], {}
            for p in _SBI_EXPORTS:
                recs = _load_process_recs(db, p, rd, df_, dt_)
                by_date = {}
                for r in recs:
                    by_date.setdefault(r.get("recon_date") or "", []).append(r)
                for d in sorted(by_date):
                    row = _proc_overview(p, by_date[d]); row["date"] = d
                    grid.append(row)
                totals[p] = _proc_overview(p, recs)
            _sheet(w, "By Date x Process", grid,
                   ["date", "process", "total", "matched", "open", "match_rate_pct",
                    "amount_total", "amount_matched", "amount_open"])
            _sheet(w, "Totals by Process", [totals[p] for p in _SBI_EXPORTS],
                   ["process", "sheet", "total", "matched", "open", "match_rate_pct",
                    "amount_total", "amount_matched", "amount_open", "notes"])

        elif t == "ko_wise":
            def _agg(recs, key, amt_field, ok_states):
                out = {}
                for r in recs:
                    k = r.get(key) or "(blank)"
                    a = out.setdefault(k, {"ko_csp": k, "total": 0, "matched": 0, "open": 0,
                                           "amount_total": 0.0, "amount_open": 0.0})
                    a["total"] += 1
                    ok = (r.get("match_status") or r.get("status")) in ok_states
                    a["matched"] += 1 if ok else 0
                    a["open"] += 0 if ok else 1
                    amt = r.get(amt_field) or r.get("bank_amount") or 0
                    a["amount_total"] = round(a["amount_total"] + amt, 2)
                    if not ok:
                        a["amount_open"] = round(a["amount_open"] + amt, 2)
                return out
            p01r = _load_process_recs(db, "p01", rd, df_, dt_)
            p02a = _agg(_load_process_recs(db, "p02", rd, df_, dt_), "ko_id", "bank_amount",
                        ("Matched", "Manual_Matched"))
            p03a = _agg(_load_process_recs(db, "p03", rd, df_, dt_), "csp_code", "txn_amount",
                        ("Matched", "Manual_Matched"))
            p01a = {}
            for r in p01r:
                k = r.get("ko_id") or "(blank)"
                a = p01a.setdefault(k, {"ko_csp": k, "wallet_withdrawn": 0.0, "bank_settled": 0.0,
                                        "difference": 0.0, "days": 0, "days_credited": 0})
                a["wallet_withdrawn"] = round(a["wallet_withdrawn"] + (r.get("wallet_withdrawn") or 0), 2)
                a["bank_settled"] = round(a["bank_settled"] + (r.get("bank_settled") or 0), 2)
                a["difference"] = round(a["difference"] + (r.get("difference") or 0), 2)
                a["days"] += 1
                a["days_credited"] += 1 if r.get("status") == "CREDITED" else 0
            all_kos = sorted(set(p01a) | set(p02a) | set(p03a))
            overview = []
            for k in all_kos:
                o1, o2, o3 = p01a.get(k, {}), p02a.get(k, {}), p03a.get(k, {})
                overview.append({
                    "ko_csp": k,
                    "p01_wallet": o1.get("wallet_withdrawn", 0), "p01_settled": o1.get("bank_settled", 0),
                    "p01_diff": o1.get("difference", 0),
                    "p02_total": o2.get("total", 0), "p02_open": o2.get("open", 0),
                    "p02_open_amt": o2.get("amount_open", 0),
                    "p03_total": o3.get("total", 0), "p03_open": o3.get("open", 0),
                    "p03_open_amt": o3.get("amount_open", 0),
                    "total_open_amt": round((o2.get("amount_open", 0) or 0) + (o3.get("amount_open", 0) or 0), 2),
                })
            overview.sort(key=lambda r: -r["total_open_amt"])
            _sheet(w, "KO Overview", overview,
                   ["ko_csp", "p01_wallet", "p01_settled", "p01_diff", "p02_total", "p02_open",
                    "p02_open_amt", "p03_total", "p03_open", "p03_open_amt", "total_open_amt"])
            _sheet(w, "P01 by KO", sorted(p01a.values(), key=lambda r: r["ko_csp"]),
                   ["ko_csp", "wallet_withdrawn", "bank_settled", "difference", "days", "days_credited"])
            _sheet(w, "P02 by KO", sorted(p02a.values(), key=lambda r: -r["amount_open"]),
                   ["ko_csp", "total", "matched", "open", "amount_total", "amount_open"])
            _sheet(w, "P03 by CSP", sorted(p03a.values(), key=lambda r: -r["amount_open"]),
                   ["ko_csp", "total", "matched", "open", "amount_total", "amount_open"])

        elif t == "unified":
            res = get_unified(recon_date=rd, upload_date=None, side=None, status=None,
                              process=None, search=None, page=1, page_size=10 ** 9,
                              db=db, current_user=current_user) if rd else {"rows": []}
            cols = ["side", "source", "ref", "ko_csp", "amount", "date", "drcr", "status",
                    "process", "counterpart", "also_p03", "src_code", "src_note", "narration"]
            _sheet(w, "All Entries", res["rows"], cols)
            sc = res.get("status_counts") or {}
            _sheet(w, "Status Counts",
                   [{"status": k, "entries": v} for k, v in sorted(sc.items(), key=lambda x: -x[1])],
                   ["status", "entries"])

        elif t == "p01_lines":
            res = get_p01_lines(recon_date=rd, upload_date=None, ko_id=None, status=None,
                                db=db, current_user=current_user) if rd else {"kos": []}
            kos, wlines, slines = [], [], []
            for g in res["kos"]:
                kos.append({k: g.get(k) for k in ("ko_id", "status", "wallet_withdrawn", "bank_settled",
                                                  "difference", "deduct_date", "bank_txn_date",
                                                  "src_code", "src_note")})
                for x in g.get("withdrawals") or []:
                    wlines.append({"ko_id": g["ko_id"], **x})
                for x in g.get("settlements") or []:
                    slines.append({"ko_id": g["ko_id"], **x})
            _sheet(w, "KO Summary", kos, ["ko_id", "status", "wallet_withdrawn", "bank_settled",
                                          "difference", "deduct_date", "bank_txn_date", "src_code", "src_note"])
            _sheet(w, "Withdrawal Lines", wlines, ["ko_id", "amount", "txn_date", "datetime", "configured_by"])
            _sheet(w, "Settlement Lines", slines, ["ko_id", "amount", "txn_date", "deduct_date", "ref", "description"])

        elif t == "reversals":
            recs = [r for r in _load_process_recs(db, "p02", rd, df_, dt_, with_bank_desc=True)
                    if r.get("match_status") == "Reversal"]
            recs.sort(key=lambda r: (r.get("reference_number") or "", r.get("bank_type") or ""))
            _sheet(w, "Reversals", recs, _P02_XCOLS)

        elif t == "p04_actions":
            recs = _load_process_recs(db, "p04", rd, df_, dt_)
            _sheet(w, "All Actions", recs, _P04_XCOLS)
            _sheet(w, "Pending", [r for r in recs if r.get("action_required") != "NONE"
                                  and not r.get("action_done")], _P04_XCOLS)
            _sheet(w, "Done", [r for r in recs if r.get("action_done")], _P04_XCOLS)

        elif t == "src_log":
            q = _rng(db.query(SBISrcAssignment), SBISrcAssignment, rd, df_, dt_)
            rows = [{"recon_date": s.recon_date, "process": s.process, "match_key": s.match_key,
                     "src_code": s.src_code, "src_note": s.src_note, "by": s.created_by,
                     "at": str(s.created_at or "")} for s in q.order_by(SBISrcAssignment.created_at.desc()).all()]
            _sheet(w, "SRC Log", rows, ["recon_date", "process", "match_key", "src_code", "src_note", "by", "at"])

        elif t == "manual_log":
            q = _rng(db.query(SBIManualMatch), SBIManualMatch, rd, df_, dt_)
            rows = [{"recon_date": m.recon_date, "process": m.process, "match_key": m.match_key,
                     "counterpart_ref": m.counterpart_ref, "remark": m.remark, "by": m.created_by,
                     "at": str(m.created_at or "")} for m in q.order_by(SBIManualMatch.created_at.desc()).all()]
            _sheet(w, "Manual Matches", rows,
                   ["recon_date", "process", "match_key", "counterpart_ref", "remark", "by", "at"])

    output.seek(0)
    fname = f"sbi_{t}_{tag}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Recon-Date": str(tag),
            "Access-Control-Expose-Headers": "X-Recon-Date",
        },
    )


# ── Run all four processes in sequence (QoL orchestration — same code paths) ──

@router.post("/run/all")
def run_all(
    recon_date: str,
    upload_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("run_recon")),
):
    """Run P01 → P02 → P03 → P04 for one recon_date, exactly as the four individual
    buttons would (identical code paths — each keeps its own audit row). A failure in
    one process is reported but does not block the others."""
    results = {}
    for name, fn in (("p01", run_p01), ("p02", run_p02), ("p03", run_p03), ("p04", run_p04)):
        try:
            results[name] = fn(recon_date=recon_date, upload_date=upload_date,
                               db=db, current_user=current_user)
        except Exception as e:
            db.rollback()
            results[name] = {"error": str(e)[:300]}
    _audit(db, current_user, "sbi_run_all", {
        "recon_date": recon_date,
        "ok": [k for k, v in results.items() if "error" not in v],
        "failed": [k for k, v in results.items() if "error" in v]})
    return {"recon_date": recon_date, "results": results}


def _auto_run_after_upload(db, current_user):
    """Auto-recon after every SBI upload — parity with the core products' post-upload
    chain: P01→P04 run for today's recon_date and EVERY failure is swallowed, so an
    upload is never blocked (a process missing its counterpart file mid-day is
    normal; the next upload's auto-run completes it). Results land under today's
    recon_date per the upload_date≈recon_date convention (behaviour-contract #17),
    exactly as the Run All button would."""
    d = str(datetime.date.today())
    out = {}
    for name, fn in (("p01", run_p01), ("p02", run_p02), ("p03", run_p03), ("p04", run_p04)):
        try:
            fn(recon_date=d, upload_date=None, db=db, current_user=current_user)
            out[name] = "ok"
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            out[name] = f"skipped: {str(e)[:120]}"
    return {"recon_date": d, **out}


# ── Manual match (persistent overlay across re-runs) ──────────────────────────
# P02/P03 results are delete-and-recreated each run (#17), so a manual match is
# stored keyed by the row's STABLE business key and overlaid at READ time onto the
# results + exports — surviving re-runs without touching the run logic. P01/P04
# don't carry a bank↔report pairing, so manual match applies to P02 and P03.

def _manual_key(process: str, row: dict):
    """Stable business key for a result row (survives delete-and-recreate)."""
    p = (process or "").lower()
    if p == "p02":
        ref = row.get("reference_number")
        return f"{ref}|{row.get('bank_type') or ''}" if ref else None
    if p == "p03":
        csp, ref = row.get("csp_code"), row.get("ref_number")
        return f"{csp or ''}|{ref or ''}" if (csp or ref) else None
    return None


def _apply_manual_matches(db, process: str, recon_date, rows: list) -> list:
    """Overlay persisted manual matches onto serialized result dicts (read-time)."""
    p = (process or "").lower()
    if p not in ("p02", "p03") or not rows:
        return rows
    q = db.query(SBIManualMatch).filter(SBIManualMatch.process == p)
    if recon_date:
        q = q.filter(SBIManualMatch.recon_date == recon_date)
    mm = {(m.recon_date, m.match_key): m for m in q.all()}
    if not mm:
        return rows
    for r in rows:
        key = _manual_key(p, r)
        m = mm.get((r.get("recon_date"), key)) if key else None
        if m:
            r["match_status"] = "Manual_Matched"
            r["manual_remark"] = m.remark
            r["manual_counterpart"] = m.counterpart_ref
            r["manual_match_id"] = m.id
    return rows


def _src_key(process: str, row: dict):
    """Stable business key for an SRC tag (survives delete-and-recreate, per #17)."""
    p = (process or "").lower()
    if p == "p01":
        return row.get("ko_id") or None
    if p == "p02":
        ref = row.get("reference_number")
        return f"{ref}|{row.get('bank_type') or ''}" if ref else None
    if p == "p03":
        csp, ref = row.get("csp_code"), row.get("ref_number")
        return f"{csp or ''}|{ref or ''}" if (csp or ref) else None
    if p == "p04":
        return row.get("csp_code") or None
    return None


def _apply_src_assignments(db, process: str, recon_date, rows: list) -> list:
    """Overlay persisted SRC tags onto serialized result dicts (read-time).

    Adds src_code/src_note to every row (None when untagged). Survives re-runs because
    the tag lives in SBISrcAssignment keyed by the stable business key, not the row id."""
    p = (process or "").lower()
    sa = {}
    if p in ("p01", "p02", "p03", "p04") and rows:
        q = db.query(SBISrcAssignment).filter(SBISrcAssignment.process == p)
        if recon_date:
            q = q.filter(SBISrcAssignment.recon_date == recon_date)
        sa = {(a.recon_date, a.match_key): a for a in q.all()}
    for r in rows:
        key = _src_key(p, r)
        a = sa.get((r.get("recon_date"), key)) if key else None
        r["src_code"] = a.src_code if a else None
        r["src_note"] = a.src_note if a else None
    return rows


class ManualMatchIn(BaseModel):
    process: str
    result_id: str
    counterpart_ref: Optional[str] = None
    remark: str = ""


@router.post("/manual-match")
def create_manual_match(
    body: ManualMatchIn,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("src_assign")),
):
    """Manually resolve an unmatched P02/P03 row. Persists across re-runs (overlay)."""
    p = (body.process or "").lower()
    model = {"p02": SBIP02Result, "p03": SBIP03Result}.get(p)
    if not model:
        raise HTTPException(status_code=400, detail="Manual match supports process p02 or p03")
    if len((body.remark or "").strip()) < 5:
        raise HTTPException(status_code=400, detail="A remark (≥5 characters) is required")
    row = db.query(model).filter(model.id == body.result_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Result row not found")
    row_dict = {c.name: getattr(row, c.name) for c in model.__table__.columns}
    key = _manual_key(p, row_dict)
    if not key:
        raise HTTPException(status_code=400, detail="Row has no stable key to match on")
    existing = db.query(SBIManualMatch).filter(
        SBIManualMatch.recon_date == row.recon_date,
        SBIManualMatch.process == p,
        SBIManualMatch.match_key == key,
    ).first()
    if existing:
        existing.counterpart_ref = body.counterpart_ref
        existing.remark = body.remark.strip()
        existing.created_by = current_user.username
        mm = existing
    else:
        mm = SBIManualMatch(recon_date=row.recon_date, process=p, match_key=key,
                            counterpart_ref=body.counterpart_ref, remark=body.remark.strip(),
                            created_by=current_user.username)
        db.add(mm)
    try:
        db.add(AuditLog(user_id=current_user.id, username=current_user.username,
                        action="sbi_manual_match", action_type="human",
                        entity_type="sbi", entity_id=mm.id,
                        detail=json.dumps({"process": p, "recon_date": row.recon_date,
                                           "match_key": key, "counterpart_ref": body.counterpart_ref,
                                           "remark": body.remark.strip()})))
    except Exception:
        pass
    db.commit()
    return {"id": mm.id, "match_status": "Manual_Matched", "match_key": key}


@router.delete("/manual-match/{mm_id}")
def delete_manual_match(
    mm_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("src_assign")),
):
    """Undo a manual match (reverts the row to its algorithm-computed status)."""
    mm = db.query(SBIManualMatch).filter(SBIManualMatch.id == mm_id).first()
    if not mm:
        raise HTTPException(status_code=404, detail="Manual match not found")
    db.delete(mm)
    try:
        db.add(AuditLog(user_id=current_user.id, username=current_user.username,
                        action="sbi_manual_unmatch", action_type="human",
                        entity_type="sbi", entity_id=mm_id,
                        detail=json.dumps({"process": mm.process, "match_key": mm.match_key})))
    except Exception:
        pass
    db.commit()
    return {"deleted": mm_id}


@router.get("/manual-match")
def list_manual_matches(
    recon_date: Optional[str] = None,
    process: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    q = db.query(SBIManualMatch)
    if recon_date: q = q.filter(SBIManualMatch.recon_date == recon_date)
    if process:    q = q.filter(SBIManualMatch.process == process.lower())
    rows = q.order_by(SBIManualMatch.created_at.desc()).all()
    return {"rows": [{"id": m.id, "recon_date": m.recon_date, "process": m.process,
                      "match_key": m.match_key, "counterpart_ref": m.counterpart_ref,
                      "remark": m.remark, "by": m.created_by, "at": str(m.created_at)} for m in rows],
            "count": len(rows)}


# ── SRC disposition (overlay; parity with core-ledger /recon/assign-src) ───────
class SBISRCIn(BaseModel):
    process: str       # p01 | p02 | p03 | p04
    result_id: str
    src_code: str
    src_note: str = ""


@router.post("/assign-src")
def assign_src(
    body: SBISRCIn,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("src_assign")),
):
    """Tag an SBI result row with an SRC code + note. Persists across re-runs (overlay)."""
    from routes.recon import SRC_CODES
    p = (body.process or "").lower()
    model = {"p01": SBIP01Result, "p02": SBIP02Result, "p03": SBIP03Result, "p04": SBIP04Result}.get(p)
    if not model:
        raise HTTPException(status_code=400, detail="process must be one of p01..p04")
    if body.src_code not in SRC_CODES:
        raise HTTPException(status_code=400, detail=f"Invalid SRC code. Allowed: {SRC_CODES}")
    row = db.query(model).filter(model.id == body.result_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Result row not found")
    row_dict = {c.name: getattr(row, c.name) for c in model.__table__.columns}
    key = _src_key(p, row_dict)
    if not key:
        raise HTTPException(status_code=400, detail="Row has no stable key to tag")
    existing = db.query(SBISrcAssignment).filter(
        SBISrcAssignment.recon_date == row.recon_date,
        SBISrcAssignment.process == p,
        SBISrcAssignment.match_key == key,
    ).first()
    if existing:
        existing.src_code = body.src_code
        existing.src_note = (body.src_note or "")[:500]
        existing.created_by = current_user.username
        sa = existing
    else:
        sa = SBISrcAssignment(recon_date=row.recon_date, process=p, match_key=key,
                              src_code=body.src_code, src_note=(body.src_note or "")[:500],
                              created_by=current_user.username)
        db.add(sa)
    try:
        db.add(AuditLog(user_id=current_user.id, username=current_user.username,
                        action="sbi_assign_src", action_type="human",
                        entity_type="sbi", entity_id=sa.id,
                        detail=json.dumps({"process": p, "recon_date": row.recon_date,
                                           "match_key": key, "src_code": body.src_code,
                                           "src_note": body.src_note})))
    except Exception:
        pass
    db.commit()
    return {"id": sa.id, "src_code": body.src_code, "match_key": key}


@router.delete("/assign-src/{sa_id}")
def delete_src_assignment(
    sa_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("src_assign")),
):
    """Remove an SBI SRC tag (revert the row to untagged)."""
    sa = db.query(SBISrcAssignment).filter(SBISrcAssignment.id == sa_id).first()
    if not sa:
        raise HTTPException(status_code=404, detail="SRC assignment not found")
    db.delete(sa)
    try:
        db.add(AuditLog(user_id=current_user.id, username=current_user.username,
                        action="sbi_unassign_src", action_type="human",
                        entity_type="sbi", entity_id=sa_id,
                        detail=json.dumps({"process": sa.process, "match_key": sa.match_key})))
    except Exception:
        pass
    db.commit()
    return {"deleted": sa_id}


@router.get("/summary")
def get_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Dashboard roll-up for SBI Kiosk. Data-presence counts + an overall recon
    health rate across the four processes (P01 settlement, P02 bank↔txn,
    P03 CSP↔txn↔bank, P04 wallet balance). Date-range aware on the result tables."""
    from sqlalchemy import func as _f
    GOOD = {"credited", "matched", "reconciled", "ok", "none", "no_action",
            "balanced", "success", "settled", "no action"}

    counts = {
        "bank_statement":  db.query(SBIBankTransaction).count(),
        "txn_reports":     db.query(SBITxnReport).count(),
        "ko_limits":       db.query(SBIKOLimits).count(),
        "ko_cash_holding": db.query(SBIKOCashHolding).count(),
        "limit_failures":  db.query(SBILimitFailure).count(),
        "csp_master":      db.query(SBICSPMaster).count(),
    }

    def _proc(model, status_col):
        q = db.query(status_col, _f.count())
        if date_from: q = q.filter(model.recon_date >= date_from)
        if date_to:   q = q.filter(model.recon_date <= date_to)
        by = {(s or "").strip().lower(): c for s, c in q.group_by(status_col).all()}
        total   = sum(by.values())
        matched = sum(c for s, c in by.items() if s in GOOD)
        return {"total": total, "matched": matched,
                "exceptions": total - matched, "by_status": by}

    p01 = _proc(SBIP01Result, SBIP01Result.status)
    p02 = _proc(SBIP02Result, SBIP02Result.match_status)
    p03 = _proc(SBIP03Result, SBIP03Result.match_status)
    p04 = _proc(SBIP04Result, SBIP04Result.action_required)

    tot     = p01["total"] + p02["total"] + p03["total"] + p04["total"]
    matched = p01["matched"] + p02["matched"] + p03["matched"] + p04["matched"]
    return {
        "counts":      counts,
        "has_data":    any(counts.values()),
        "processes":   {"p01": p01, "p02": p02, "p03": p03, "p04": p04},
        "total":       tot,
        "matched":     matched,
        "exceptions":  tot - matched,
        "match_rate":  round(matched / tot * 100, 1) if tot else None,
    }


@router.get("/upload-status")
def get_upload_status(
    upload_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Show count of uploaded records per data type for the given date."""
    today = upload_date or str(datetime.date.today())
    return {
        "upload_date": today,
        "bank_statement":  db.query(SBIBankTransaction).filter(SBIBankTransaction.upload_date == today).count(),
        "txn_reports":     db.query(SBITxnReport).filter(SBITxnReport.upload_date == today).count(),
        "ko_limits":       db.query(SBIKOLimits).filter(SBIKOLimits.upload_date == today).count(),
        "ko_cash_holding": db.query(SBIKOCashHolding).filter(SBIKOCashHolding.upload_date == today).count(),
        "limit_failures":  db.query(SBILimitFailure).filter(SBILimitFailure.upload_date == today).count(),
        "csp_master":      db.query(SBICSPMaster).filter(SBICSPMaster.upload_date == today).count(),
    }


@router.delete("/clear")
def clear_sbi_data(
    table: Optional[str] = Query(None, description="bank|txn|limits|cash|failures|csp|p01|p02|p03|p04|all"),
    upload_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("upload")),
):
    """Clear SBI data (all tables or specific table, optionally by upload_date)."""
    tables = {
        "bank": SBIBankTransaction, "txn": SBITxnReport,
        "limits": SBIKOLimits, "cash": SBIKOCashHolding,
        "failures": SBILimitFailure, "csp": SBICSPMaster,
        "p01": SBIP01Result, "p02": SBIP02Result,
        "p03": SBIP03Result, "p04": SBIP04Result,
    }
    to_clear = tables if table in (None, "all") else {table: tables[table]} if table in tables else {}
    if not to_clear:
        raise HTTPException(status_code=400, detail=f"Unknown table: {table}")
    counts = {}
    for name, model in to_clear.items():
        q = db.query(model)
        if upload_date and hasattr(model, 'upload_date'):
            q = q.filter(model.upload_date == upload_date)
        counts[name] = q.delete()
        db.commit()
    return {"deleted": counts}
