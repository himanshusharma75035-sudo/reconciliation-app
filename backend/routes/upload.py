import logging
import os, json, shutil, uuid, re, datetime, time
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Form
from sqlalchemy.orm import Session
from typing import Optional
from models.database import (
    get_db, UploadSession, Transaction, UploadStatus, generate_id,
    User, ColumnMappingTemplate, AuditLog, UploadHistory,
    PIIntegrityCheck, SettlementBankConfig
)
from core.auth import get_current_user, require_permission
from core.pdf_converter import load_file_to_dataframe

logger = logging.getLogger("eko_recon")

router = APIRouter(prefix="/api/upload", tags=["upload"])

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# "description" is special — mapped value gets auto-parsed for TID + Tracking + UTR
STANDARD_FIELDS = [
    "eko_tid", "tracking_number", "utr_number",
    "amount", "status", "transaction_date", "description"
]

# Values treated as null/missing in source files (e.g. MySQL dumps)
NULL_VALUES = {'\\N', 'NULL', 'null', 'None', 'none', 'NA', 'N/A', ''}

# ── Bank statement format presets ────────────────────────────────────────────
# Each entry defines a known bank format by its column signature.
# "mapping" pre-fills the column-mapping dropdowns in Step 2.
# Amount columns are intentionally left unmapped so _auto_detect_amount
# can correctly handle separate Debit/Credit columns per format.
# Add new bank formats here as they are discovered.
BANK_FORMAT_PRESETS = {
    "fino_bank": {
        "label":         "Fino PTA Bank Statement",
        "partners":      ["fino"],
        "sides":         ["bank"],
        # All of these must be present (lowercase) for the format to match
        "signature_cols": {"description", "debits", "credits", "ending balance"},
        "mapping": {
            "transaction_date": "Transaction Effective Date",
            "description":      "Description",
            # eko_tid / tracking_number / utr_number: auto-extracted from Description
            # amount: deliberately omitted — auto-detect handles Debits/Credits pair
        },
        # These column names tell _auto_detect_amount where to find DR/CR amounts
        "debit_col":  "Debits",
        "credit_col": "Credits",
        # FREC: Fino PTA must have a "Description" column containing IMPS/NEFT patterns
        "frec": {
            "col":   "Description",
            "check": "col_exists",
        },
    },
    "fino_internal": {
        "label":   "Fino Simplibank DMT Dump",
        "partners": ["fino"],
        "sides":    ["internal"],
        # Signature columns — stripped + lowercased. All must be present.
        # Fino dump has leading/trailing spaces in column headers; _detect_bank_format
        # strips before matching, so list the stripped lowercase versions here.
        "signature_cols": {"eko_trxn_id", "trackingnumber", "utrnumber", "debit_credit"},
        "mapping": {
            "eko_tid":          "eko_trxn_id",    # stripped from ' eko_trxn_id '
            "tracking_number":  "TrackingNumber",  # stripped from ' TrackingNumber '
            "utr_number":       "UTRNUMBER",       # stripped from ' UTRNUMBER '
            "transaction_date": "Transaction_Date",# stripped from ' Transaction_Date '
            # amount: not mapped — _auto_detect_amount finds 'amount' + 'debit_credit'
            # source: ' Source ' is all NULL — do not use as filter
        },
    },
    "airtel_bank": {
        "label":         "Airtel Payments Bank Statement",
        "partners":      ["airtel"],
        "sides":         ["bank"],
        # Columns that must all be present (lowercase) for this format to auto-detect
        "signature_cols": {
            "date", "time", "apb_txn_id", "partner_txn_id", "rrn",
            "transaction amount", "original amount", "transaction type", "description"
        },
        "mapping": {
            "transaction_date": "Date",
            "description":      "Description",
            "eko_tid":          "Partner_txn_id",
            "tracking_number":  "RRN",
            "amount":           "Original Amount",
            "utr_number":       "APB_TXN_id",
        },
        "dr_cr_col": "Transaction Type",
        # FREC: Airtel statement must have the unique APB_TXN_id column
        "frec": {
            "col":   "APB_TXN_id",
            "check": "col_exists",
        },
    },
    # ── AePS Cashout — Fingpay transaction report ─────────────────────────────
    # Source: Fingpay API response export (cw-success-failure file)
    # Matching: eko_tid (Merchant Transaction Id) + tracking_number (Response Rrn)
    "aeps_fingpay": {
        "label":   "AePS Fingpay Report",
        "partners": ["aeps"],
        "sides":    ["bank"],
        "signature_cols": {
            "merchant transaction id", "response rrn", "fingpay transaction id",
            "transaction amount", "requested timestamp"
        },
        "mapping": {
            "eko_tid":          "Merchant Transaction Id",
            "tracking_number":  "Response Rrn",
            "amount":           "Transaction Amount",
            "transaction_date": "Requested Timestamp",
            "status":           "Status Message",
        },
        # FREC: confirm file is an AePS Fingpay report by checking a known header
        "frec": {
            "col": "Merchant Transaction Id",   # column that must exist and be non-empty
            "check": "col_exists",              # check type: col_exists | cell_contains
        },
    },
    # ── AePS Cashout — Simplibank dump (filtered to AePS Cashout rows) ────────
    "aeps_dump": {
        "label":   "AePS Simplibank Dump",
        "partners": ["aeps"],
        "sides":    ["internal"],
        "signature_cols": {"eko_trxn_id", "trackingnumber", "typename", "business_vertical"},
        "typename_filter": "AePS Cashout",   # only ingest rows where TYPENAME == this value
        "mapping": {
            "eko_tid":          "eko_trxn_id",
            "tracking_number":  "TrackingNumber",
            "utr_number":       "UTRNUMBER",
            "amount":           "Amount",
            "transaction_date": "Transaction_Date",
            "status":           "Status",
        },
    },
    # ── Accept Payment (PG) — PayU settlement report ──────────────────────────
    # Source: PayU settlement export (settlementReport_*.xlsx)
    # Matching: eko_tid (Merchant Txn ID) + amount (gross Amount — same as dump)
    # net_amount stored separately for PG settlement summary reporting
    "pg_payu": {
        "label":   "PG PayU Settlement Report",
        "partners": ["pg"],
        "sides":    ["bank"],
        "signature_cols": {
            "merchant txn id", "merchant utr", "payu id",
            "amount", "net amount", "settlement date"
        },
        "mapping": {
            "eko_tid":          "Merchant Txn ID",
            "utr_number":       "Merchant UTR",
            "amount":           "Amount",
            "transaction_date": "AddedOn",
            "status":           "Status",
        },
        # Special: net_amount is ingested separately from this column
        "net_amount_col": "Net Amount",
        # FREC: confirm file is a PayU report by checking a known header column
        "frec": {
            "col": "Merchant Txn ID",   # column that must be present
            "check": "col_exists",
        },
        # AS=SD integrity check columns (PayU provides per-row fee breakdown)
        "integrity_check": {
            "gross_col":      "Amount",      # gross transaction amount
            "net_col":        "Net Amount",  # net after fees (= AS per row)
            "fee_col":        "fees",        # gateway fees
            "gst_col":        "tax",         # GST on fees (if column present)
            "status_col":     "Status",      # only include settled/success rows
            "settled_status": ["captured", "success"],  # values that count as settled
        },
    },
    # ── Axis Bank Statement ───────────────────────────────────────────────────
    # Source: Axis Bank account statement CSV (EKO's payout account)
    # Header is preceded by ~16 metadata rows — auto-skipped by load_file_to_dataframe
    # Particulars format:
    #   IMPS/P2A/{12-digit tracking}/{beneficiary}/{acc}/{bank}/  → tracking_number
    #   NEFT/AXISCN{bank_ref}/{eko_tid}/{beneficiary}/{ifsc}      → eko_tid
    # DR rows = outgoing DMT payouts. CR rows = fund inflows (not reconciled).
    "axis_bank": {
        "label":   "Axis Bank Statement",
        "partners": ["axis"],
        "sides":    ["bank"],
        "signature_cols": {
            "s.no", "transaction date (dd/mm/yyyy)", "particulars",
            "amount(inr)", "debit/credit", "balance(inr)"
        },
        "mapping": {
            "transaction_date": "Transaction Date (dd/mm/yyyy)",
            "description":      "Particulars",
            # tracking_number and eko_tid auto-extracted from description via _parse_description
            # amount: auto-detected from Amount(INR) + Debit/Credit column
        },
        "debit_credit_col": "Debit/Credit",   # DR/CR indicator for amount detection
        "amount_col": "Amount(INR)",           # single amount column (needs \t + comma strip)
        # FREC: Axis statement contains account number in a metadata row — checked via header keyword
        "frec": {
            "col": "Particulars",   # must be present as a column name
            "check": "col_exists",
        },
    },
    # ── Axis internal dump (from mixed Simplibank dump, source='Axis') ────────
    "axis_internal": {
        "label":   "Axis Simplibank Dump",
        "partners": ["axis"],
        "sides":    ["internal"],
        "signature_cols": {"eko_trxn_id", "trackingnumber", "debit_credit", "source"},
        "source_filter": "Axis",   # filter by source column for mixed dumps
        "mapping": {
            "eko_tid":          "eko_trxn_id",
            "tracking_number":  "TrackingNumber",
            "utr_number":       "UTRNUMBER",
            "transaction_date": "Transaction_Date",
            "status":           "Status",
        },
    },
    # ── Levin statement ───────────────────────────────────────────────────────
    # Placeholder — statement format TBD when Levin provides sample (Levin is a
    # partner, not a bank — naming per management decision, Jun 2026)
    # ── Levin internal dump (from mixed Simplibank dump, source='Levin') ─────
    "levin_internal": {
        "label":   "Levin Simplibank Dump",
        "partners": ["levin"],
        "sides":    ["internal"],
        "signature_cols": {"eko_trxn_id", "trackingnumber", "debit_credit", "source"},
        "source_filter": "Levin",
        "mapping": {
            "eko_tid":          "eko_trxn_id",
            "tracking_number":  "TrackingNumber",
            "utr_number":       "UTRNUMBER",
            "transaction_date": "Transaction_Date",
            "status":           "Status",
        },
    },
    # ── Levin bank statement (MoneyTransactionReport) ─────────────────────────
    # Source: Levin DMT MoneyTransactionReport (xlsx). Rows are outgoing IMPS/NEFT
    # payouts. Matching: utr_number (UTR) OR eko_tid (Client_Ref_Id with the
    # "EKOI" prefix stripped → equals the internal dump's eko_trxn_id).
    "levin_bank": {
        "label":   "Levin Bank Statement",
        "partners": ["levin"],
        "sides":    ["bank"],
        "signature_cols": {
            "date & time", "tx id", "product", "amount",
            "mode", "status", "utr", "client_ref_id",
        },
        "mapping": {
            "eko_tid":          "Client_Ref_Id",
            "utr_number":       "UTR",
            "amount":           "Amount",
            "transaction_date": "Date & Time",
            "status":           "Status",
        },
        # Client_Ref_Id is "EKOI<tid>" — strip the prefix during ingestion so it
        # matches the internal eko_trxn_id. (Applied in confirm_mapping.)
        "eko_tid_strip_prefix": "EKOI",
        "frec": {"col": "Client_Ref_Id", "check": "col_exists"},
    },
    # ── Accept Payment (PG) — Simplibank dump (filtered to Accept Payment rows)
    "pg_dump": {
        "label":   "PG Simplibank Dump",
        "partners": ["pg"],
        "sides":    ["internal"],
        "signature_cols": {"eko_trxn_id", "trackingnumber", "typename", "business_vertical"},
        "typename_filter": "Accept Payment",  # only ingest rows where TYPENAME == this value
        "mapping": {
            "eko_tid":          "eko_trxn_id",
            "amount":           "Amount",
            "transaction_date": "Transaction_Date",
            "status":           "Status",
        },
    },

    # ── QR Collection — partner bank (Paypoint transaction export) ───────────────
    # Source: ExportTransactionList_*.xlsx from Paypoint dashboard
    # Matching: OrderId ↔ TrackingNumber (internal) + RRN ↔ UTRNUMBER (internal)
    # Failed rows: Txn Status in FAILED_STATUSES → auto-classified as failed
    # Credit card rows: Payer Account Type == "CREDIT" → fees stored via net_amount
    "qr_bank": {
        "label":   "QR Transaction Report",
        "partners": ["qr"],
        "sides":    ["bank"],
        "signature_cols": {
            "orderid", "rrn", "transaction id", "vpa address",
            "payer account type", "net amount"
        },
        "mapping": {
            "tracking_number":  "OrderId",              # primary match = internal TrackingNumber
            "utr_number":       "RRN",                  # secondary match = internal UTRNUMBER
            "amount":           "Amount",
            "transaction_date": "Transaction Date",
            "status":           "Txn Status",
        },
        "net_amount_col": "Net Amount",                 # store per-txn net (after CC fees)
    },

    # ── QR Collection — Simplibank internal dump ──────────────────────────────────
    # Source: Combined Simplibank dump; rows filtered by TYPENAME = "QR Collection"
    # All rows are CR (credit to CSP). Status = Success.
    "qr_dump": {
        "label":   "QR Simplibank Dump",
        "partners": ["qr"],
        "sides":    ["internal"],
        "signature_cols": {"eko_trxn_id", "trackingnumber", "typename", "business_vertical"},
        "typename_filter": "QR Collection",
        "mapping": {
            "eko_tid":          "eko_trxn_id",
            "tracking_number":  "TrackingNumber",
            "utr_number":       "UTRNUMBER",
            "amount":           "Amount",
            "transaction_date": "Transaction_Date",
            "status":           "Status",
        },
    },

    # ── Digikhata (PPI Wallet Load) — partner bank report ────────────────────────
    # Source: Digikhata bank report (all Payment / eKYC Charge rows)
    # Matching: eko_tid (Txn Chain Ref Id) + amount
    # KYC rows: Product == "eKYC Charge", no Txn Chain Ref Id — classified separately
    "digikhata_bank": {
        "label":   "Digikhata Bank Report",
        "partners": ["digikhata"],
        "sides":    ["bank"],
        "signature_cols": {
            "transaction ledger id", "txn chain ref id",
            "transaction reference id", "amount (rs.)"
        },
        "mapping": {
            "eko_tid":          "Txn Chain Ref Id",
            "tracking_number":  "Transaction Reference ID",
            "amount":           "Amount (Rs.)",
            "transaction_date": "Date",
        },
        "kyc_product_col":  "Product",          # column to check for KYC charges
        "kyc_product_val":  "eKYC Charge",      # value that marks a KYC row
    },

    # ── Digikhata (PPI Wallet Load) — Simplibank internal dump ────────────────────
    # Source: Combined Simplibank dump; rows filtered by TYPENAME
    # ALL sub-entries per TID are ingested (main load + CCF + GST + commission + TDS)
    "digikhata_dump": {
        "label":   "Digikhata Simplibank Dump",
        "partners": ["digikhata"],
        "sides":    ["internal"],
        "signature_cols": {"eko_trxn_id", "trackingnumber", "typename", "business_vertical"},
        "typename_filter": "Digi Khata Load Wallet",
        "mapping": {
            "eko_tid":          "eko_trxn_id",
            "tracking_number":  "TrackingNumber",
            "utr_number":       "UTRNUMBER",
            "amount":           "Amount",
            "transaction_date": "Transaction_Date",
            "status":           "Status",
        },
    },

    # ── Indonepal — partner bank report ──────────────────────────────────────────
    # Source: Indonepal transaction report (2 metadata rows before real header)
    # The Excel multi-row header is handled by load_file_to_dataframe auto-detection.
    # Matching: eko_tid (PartnerPinNo)
    # Total/summary rows (PartnerPinNo blank) are skipped during ingestion.
    "indonepal_bank": {
        "label":   "Indonepal Bank Report",
        "partners": ["indonepal"],
        "sides":    ["bank"],
        "signature_cols": {
            "partnerpinno", "txnid", "operationstatus", "payamt", "sendamt"
        },
        "mapping": {
            "eko_tid":          "PartnerPinNo",
            "tracking_number":  "TxnId",
            "amount":           "SendAmt",
            "transaction_date": "SendDate",
            "status":           "OperationStatus",
        },
        "skip_blank_eko_tid": True,   # skip summary/total rows with no PartnerPinNo
    },

    # ── Indonepal — Simplibank internal dump ──────────────────────────────────────
    # Source: Combined Simplibank dump; rows filtered by TYPENAME
    # Success: DR only. Failed: DR + CR pair (fund reversal — both ingested)
    "indonepal_dump": {
        "label":   "Indonepal Simplibank Dump",
        "partners": ["indonepal"],
        "sides":    ["internal"],
        "signature_cols": {"eko_trxn_id", "trackingnumber", "typename", "business_vertical"},
        "typename_filter": "Indo-Nepal Money Transfer",
        "mapping": {
            "eko_tid":          "eko_trxn_id",
            "tracking_number":  "TrackingNumber",
            "utr_number":       "UTRNUMBER",
            "amount":           "Amount",
            "transaction_date": "Transaction_Date",
            "status":           "Status",
        },
    },
}


def _detect_bank_format(partner: str, side: str, columns: list) -> Optional[dict]:
    """Return the matching preset dict if columns match a known bank format, else None."""
    col_set = {c.strip().lower() for c in columns}
    for preset in BANK_FORMAT_PRESETS.values():
        if partner in preset["partners"] and side in preset["sides"]:
            if preset["signature_cols"].issubset(col_set):
                return preset
    return None


def _check_frec(df, preset: dict, partner: str, side: str) -> Optional[str]:
    """
    FREC — File Recognition Check (industry-standard reconciliation control).
    Reads a known cell/header from the parsed DataFrame to confirm the file
    belongs to the declared bank/format.

    check types:
      "col_exists"     — the named column must be present in the file (column header check)
      "cell_contains"  — a specific cell (row, col) must contain a known substring

    Returns an error string if the check fails, None if it passes or no FREC configured.
    """
    frec = preset.get("frec")
    if not frec:
        return None   # No FREC configured for this preset — pass through

    check_type = frec.get("check", "col_exists")
    col_name   = frec.get("col", "")
    cols_lower = {c.strip().lower() for c in df.columns}

    if check_type == "col_exists":
        # Verify the named column is present — confirms the file is the right type
        if col_name.strip().lower() not in cols_lower:
            return (
                f"FREC failed: expected column '{col_name}' not found in uploaded file. "
                f"This file does not appear to be a valid {preset['label']}. "
                f"Please verify you are uploading the correct file for partner '{partner}' ({side} side)."
            )

    elif check_type == "cell_contains":
        # Read a specific cell and verify it contains the expected value
        row_idx  = frec.get("row", 0)      # 0-based row index
        expected = frec.get("contains", "")
        try:
            # Find the target column (case-insensitive)
            target_col = next(
                (c for c in df.columns if c.strip().lower() == col_name.strip().lower()), None
            )
            if target_col is None:
                return (
                    f"FREC failed: column '{col_name}' not found in file. "
                    f"Cannot verify this file belongs to partner '{partner}'."
                )
            cell_val = str(df[target_col].iloc[row_idx]).strip() if row_idx < len(df) else ""
            if expected and expected.lower() not in cell_val.lower():
                return (
                    f"FREC failed: expected '{expected}' in row {row_idx+1} of column '{col_name}', "
                    f"but found '{cell_val}'. "
                    f"This file may not belong to the declared bank/format for partner '{partner}'."
                )
        except Exception as e:
            return f"FREC check error for partner '{partner}': {e}"

    return None   # FREC passed


def _check_wlr(declared_partner: str, declared_side: str, columns: list) -> Optional[str]:
    """
    WLR — Wrong Record detection (industry-standard reconciliation control).
    If the file's column signature matches a DIFFERENT partner/format than declared,
    reject it with a clear error rather than silently creating unmatched items.

    Classic case: Axis CDM file uploaded under regular Axis DMT account.
    The CDM file has different signature columns, so it would be detected as the wrong format.

    Returns a descriptive error string if WLR is triggered, else None.

    WLR is SKIPPED for:
    - partner='mixed' — combined Simplibank dumps intentionally contain multiple partners'
      columns and will always match individual-partner signatures. That is by design.
    """
    # Mixed dumps contain data for all partners — their columns will naturally match
    # individual-partner preset signatures. WLR does not apply here.
    if declared_partner == "mixed":
        return None

    col_set = {c.strip().lower() for c in columns}

    # First check: does the file match ANY preset for the declared partner/side?
    declared_match = False
    for preset in BANK_FORMAT_PRESETS.values():
        if declared_partner in preset["partners"] and declared_side in preset["sides"]:
            if preset["signature_cols"].issubset(col_set):
                declared_match = True
                break

    if declared_match:
        return None   # File matches declared format — no WLR

    # Second check: does it match a DIFFERENT partner/side preset?
    # This is the WLR condition.
    best_match_label    = None
    best_match_partners = []
    best_match_sides    = []
    best_match_count    = 0

    for preset in BANK_FORMAT_PRESETS.values():
        if preset["signature_cols"].issubset(col_set):
            match_count = len(preset["signature_cols"])
            if match_count > best_match_count:
                best_match_count    = match_count
                best_match_label    = preset["label"]
                best_match_partners = preset["partners"]
                best_match_sides    = preset["sides"]

    if best_match_label:
        return (
            f"Wrong Record (WLR): the uploaded file's column signature matches "
            f"'{best_match_label}' (partner: {best_match_partners}, side: {best_match_sides}), "
            f"but it was uploaded under partner '{declared_partner}' ({declared_side} side). "
            f"Please upload under the correct partner, or verify you selected the right file."
        )

    # File doesn't match the declared format but also doesn't match any other known format.
    # This is a format-unknown situation — return a softer warning, not a hard block.
    return None


# ── AS=SD Integrity Check helpers ─────────────────────────────────────────────

def _check_as_sd_payu(df, df_columns: list) -> Optional[dict]:
    """
    AS=SD integrity check for PayU settlement report (Item 1).
    Formula: sum(Net Amount) must equal sum(Amount - Commission - GST) across all rows.
    Tolerance: ±1 rupee total.
    Returns a result dict; None if required columns are absent.

    AS  = Actual Settlement declared in file  = sum(Net Amount)
    SD  = Settlement Due computed             = sum(Amount) - sum(fees) - sum(GST)

    This catches rounding drift, corrupt rows, or manual adjustments in the PI file.
    """
    col_map = {c.strip().lower(): c for c in df_columns}

    gross_c = col_map.get("amount")
    net_c   = col_map.get("net amount")
    fee_c   = col_map.get("fees") or col_map.get("commission") or col_map.get("msf")
    gst_c   = col_map.get("tax") or col_map.get("gst") or col_map.get("gst amount")

    if not gross_c or not net_c:
        return None   # Cannot run check without these columns

    def _sf(v):
        try:
            return float(str(v).replace(",", "").strip() or 0)
        except Exception:
            return 0.0

    total_as  = sum(_sf(v) for v in df[net_c])
    total_gross = sum(_sf(v) for v in df[gross_c])
    total_fee   = sum(_sf(v) for v in df[fee_c]) if fee_c else 0.0
    total_gst   = sum(_sf(v) for v in df[gst_c]) if gst_c else 0.0
    total_sd    = total_gross - total_fee - total_gst

    diff      = abs(total_as - total_sd)
    tolerance = 1.0   # ±1 rupee
    passed    = diff <= tolerance

    return {
        "check":         "AS=SD",
        "partner":       "pg_payu",
        "sum_net_amount":    round(total_as, 2),
        "sum_gross":         round(total_gross, 2),
        "sum_fees":          round(total_fee, 2),
        "sum_gst":           round(total_gst, 2),
        "sum_computed_sd":   round(total_sd, 2),
        "difference":        round(diff, 2),
        "tolerance":         tolerance,
        "passed":            passed,
        "message": (
            f"AS=SD PASSED: Net Amount sum ₹{total_as:,.2f} matches computed SD "
            f"(diff ₹{diff:.2f}, within ₹{tolerance} tolerance)"
            if passed else
            f"AS=SD WARNING: Net Amount sum ₹{total_as:,.2f} ≠ computed SD ₹{total_sd:,.2f} "
            f"(diff ₹{diff:.2f} exceeds ₹{tolerance} tolerance). "
            f"Check for missing fee rows or rounding errors in the PayU file."
        ),
    }


def _update_aeps_integrity_cw(
    db: Session,
    recon_date: str,
    cw_amount: float,
    cw_row_count: int,
) -> Optional[dict]:
    """
    Update PIIntegrityCheck when AePS Fingpay cw file is uploaded (Item 2).
    If T Plus already uploaded for this date, run the comparison immediately.
    Returns the current integrity check state.
    """
    from sqlalchemy.exc import IntegrityError
    try:
        record = (
            db.query(PIIntegrityCheck)
            .filter(PIIntegrityCheck.partner == "aeps",
                    PIIntegrityCheck.recon_date == recon_date)
            .first()
        )
        if record:
            record.cw_amount      = cw_amount
            record.cw_row_count   = cw_row_count
            record.cw_uploaded_at = datetime.datetime.utcnow()
        else:
            record = PIIntegrityCheck(
                id            = generate_id(),
                partner       = "aeps",
                recon_date    = recon_date,
                cw_amount     = cw_amount,
                cw_row_count  = cw_row_count,
                cw_uploaded_at = datetime.datetime.utcnow(),
                status        = "pending_tplus",
            )
            db.add(record)
        db.flush()

        # If T Plus already present → run comparison now
        if record.tplus_amount is not None:
            _run_aeps_integrity_comparison(record)

        db.commit()
        return _aeps_integrity_dict(record)
    except Exception as e:
        db.rollback()
        return {"error": str(e)}


def _update_aeps_integrity_tplus(
    db: Session,
    tplus_batch_date: str,
    tplus_amount: float,
) -> Optional[dict]:
    """
    Update PIIntegrityCheck when T Plus settlement is uploaded (Item 2).
    The T Plus date is used to find the matching cw file record.
    Runs comparison immediately if the cw file was already uploaded for this date.
    """
    try:
        record = (
            db.query(PIIntegrityCheck)
            .filter(PIIntegrityCheck.partner == "aeps",
                    PIIntegrityCheck.recon_date == tplus_batch_date)
            .first()
        )
        if record:
            record.tplus_amount      = tplus_amount
            record.tplus_batch_date  = tplus_batch_date
            record.tplus_uploaded_at = datetime.datetime.utcnow()
        else:
            record = PIIntegrityCheck(
                id                = generate_id(),
                partner           = "aeps",
                recon_date        = tplus_batch_date,
                tplus_amount      = tplus_amount,
                tplus_batch_date  = tplus_batch_date,
                tplus_uploaded_at = datetime.datetime.utcnow(),
                status            = "pending_cw",
            )
            db.add(record)
        db.flush()

        # If cw already present → run comparison now
        if record.cw_amount is not None:
            _run_aeps_integrity_comparison(record)

        db.commit()
        return _aeps_integrity_dict(record)
    except Exception as e:
        db.rollback()
        return {"error": str(e)}


def _run_aeps_integrity_comparison(record: PIIntegrityCheck):
    """Mutate record status/difference in-place after both files are available."""
    if record.cw_amount is None or record.tplus_amount is None:
        return
    diff = abs(record.cw_amount - record.tplus_amount)
    record.difference  = round(diff, 2)
    record.updated_at  = datetime.datetime.utcnow()
    record.status      = "passed" if diff <= (record.tolerance or 1.0) else "failed"


def _aeps_integrity_dict(record: PIIntegrityCheck) -> dict:
    # Build the status message None-safely and ONLY for the selected status. The
    # previous version put all four f-strings in a dict literal, so they were ALL
    # evaluated eagerly — and a PENDING record (cw_amount / tplus_amount = None)
    # crashed the whole endpoint on `{None:,.2f}`, 500-ing the AePS page.
    def _f(v, comma=True):
        if not isinstance(v, (int, float)):
            return "—"
        return f"{v:,.2f}" if comma else f"{v:.2f}"
    st = record.status
    if st == "passed":
        msg = (f"AS=SD PASSED: Fingpay ₹{_f(record.cw_amount)} matches T Plus "
               f"₹{_f(record.tplus_amount)} (diff ₹{_f(record.difference, comma=False)}).")
    elif st == "failed":
        msg = (f"AS=SD FAILED: Fingpay ₹{_f(record.cw_amount)} ≠ T Plus "
               f"₹{_f(record.tplus_amount)} (diff ₹{_f(record.difference, comma=False)}). "
               f"Investigate before settlement.")
    elif st == "pending_cw":
        msg = "Waiting for AePS Fingpay cw-success-failure file for this date."
    elif st == "pending_tplus":
        msg = "Waiting for T Plus settlement report for this date."
    else:
        msg = ""
    return {
        "partner":          record.partner,
        "recon_date":       record.recon_date,
        "cw_amount":        record.cw_amount,
        "cw_row_count":     record.cw_row_count,
        "cw_uploaded_at":   str(record.cw_uploaded_at) if record.cw_uploaded_at else None,
        "tplus_amount":     record.tplus_amount,
        "tplus_batch_date": record.tplus_batch_date,
        "tplus_uploaded_at":str(record.tplus_uploaded_at) if record.tplus_uploaded_at else None,
        "status":           record.status,
        "difference":       record.difference,
        "message":          msg,
    }


def _parse_description(desc: str) -> dict:
    """
    Auto-extract Eko TID and Tracking Number from bank description text.
    Handles:
      IMPS OUT IMPS/610508020080/3555907135        → tracking=610508020080, tid=3555907135
      IMPS OUT IMPS/606008009831                   → tracking=606008009831 (single-segment)
      FEE CHG IMPS Charges/610508020080            → tracking=610508020080 (fee description)
      FEE CHG IMPS Charges/607410029036-Reversal   → tracking=607410029036 (reversal stripped)
      NEFT/UTR123456789012/3555907135              → utr=UTR123456789012, tid=3555907135
      RTGS/UTIBR.../account/.../...               → utr from segment

    Note: "-Reversal" suffix is stripped before digit extraction so reversal rows
    get the same tracking number as their original, enabling bank-to-bank pairing.
    """
    result = {"eko_tid": None, "tracking_number": None, "utr_number": None}
    if not desc or not isinstance(desc, str):
        return result

    # Strip trailing -Reversal suffix (Fino style) so digit patterns match the original
    clean = re.sub(r'-Reversal\b.*$', '', desc, flags=re.IGNORECASE).strip()

    # Airtel reversal prefix: "REV IMPS-{APB_TXN_id}-{bank}-..."
    # Extract APB_TXN_id as tracking so reversal can be paired with original's utr_number.
    m = re.search(r'^REV\s+IMPS-(\d{8,12})-', clean, re.IGNORECASE)
    if m:
        result["tracking_number"] = m.group(1)
        result["utr_number"]      = m.group(1)
        return result

    # Airtel DR: "IMPS-{APB_TXN_id}-{bank_code}-IMPS to Account"
    # APB_TXN_id is the bank's own ref — not the Eko TID.
    # For Airtel, eko_tid and tracking come from explicit columns (Partner_txn_id, RRN).
    # We skip this description pattern so it doesn't overwrite the column-mapped values.
    # (Falls through to generic patterns below only if no column mapping provided.)

    # IMPS two-segment (Fino): IMPS/12digit/10digit
    m = re.search(r'IMPS/(\d{8,15})/(\d{8,12})', clean, re.IGNORECASE)
    if m:
        result["tracking_number"] = m.group(1)
        result["utr_number"]      = m.group(1)
        result["eko_tid"]         = m.group(2)
        return result

    # Fee charge description: "Charges/NNNNNN" (e.g. "FEE CHG IMPS Charges/607410029036")
    m = re.search(r'[Cc]harges?/(\d{8,15})', clean, re.IGNORECASE)
    if m:
        result["tracking_number"] = m.group(1)
        result["utr_number"]      = m.group(1)
        return result

    # IMPS single-segment (e.g. "IMPS OUT IMPS/606008009831")
    m = re.search(r'IMPS/(\d{8,15})', clean, re.IGNORECASE)
    if m:
        result["tracking_number"] = m.group(1)
        result["utr_number"]      = m.group(1)
        return result

    # NEFT / RTGS: UTR is alphanumeric, TID is trailing numeric
    m = re.search(r'(?:NEFT|RTGS)/([A-Z0-9]{10,22})/(\d{8,12})', clean, re.IGNORECASE)
    if m:
        result["utr_number"]      = m.group(1)
        result["tracking_number"] = m.group(1)
        result["eko_tid"]         = m.group(2)
        return result

    # Generic: /digits/digits
    m = re.search(r'/(\d{8,15})/(\d{8,12})', clean)
    if m:
        result["tracking_number"] = m.group(1)
        result["eko_tid"]         = m.group(2)
        return result

    # Last resort: any standalone 8–15 digit number (covers bare tracking in description)
    m = re.search(r'\b(\d{8,15})\b', clean)
    if m:
        result["tracking_number"] = m.group(1)
        result["utr_number"]      = m.group(1)
        return result

    return result


def _clean(val) -> Optional[str]:
    """Convert value to string, treating known null markers as None."""
    s = str(val).strip() if val is not None else ''
    return None if s in NULL_VALUES else s


# ── CSP (retailer) identity from the internal dump ───────────────────────────
# Canonical Eko internal-dump columns are "CSPCode" and "MerchantName" (the
# E-Value / BBPS engines read the very same). Lookup is case/space/underscore-
# insensitive and accepts the obvious aliases (CSP Code / CSPID, CSP Name).
# Read-only and purely additive: these are NEVER used in matching. When the
# column is absent or empty the value is left blank ("") — never inferred.
_CSP_CODE_HEADERS = {"cspcode", "cspid"}
_CSP_NAME_HEADERS = {"merchantname", "cspname"}

def _extract_csp(row_dict: dict) -> tuple:
    """Return (csp_code, csp_name) for an internal-dump row; blank when absent."""
    code = name = ""
    for k, v in row_dict.items():
        norm = re.sub(r'[^a-z0-9]', '', str(k).lower())
        if not code and norm in _CSP_CODE_HEADERS:
            code = _clean(v) or ""
        elif not name and norm in _CSP_NAME_HEADERS:
            name = _clean(v) or ""
        if code and name:
            break
    # Guard against a stray pandas 'nan' string leaking into the display field.
    if code.lower() == "nan": code = ""
    if name.lower() == "nan": name = ""
    return code, name


def _auto_detect_amount(row: dict, df_columns: list, mapped_amount_col: str, is_bank_side: bool) -> tuple:
    """
    Auto-detect amount and DR/CR indicator from a row.
    Returns (amount: float|None, dr_cr: str|None)

    Priority:
    1. Use mapped amount column if provided
    2. For bank side: find separate Debit / Credit columns
    3. For internal side: find Amount column + DEBIT_CREDIT flag
    """
    col_lower = {c.strip().lower(): c for c in df_columns}

    # 1. If user explicitly mapped an amount column, use it
    if mapped_amount_col:
        val = _safe_float(row.get(mapped_amount_col, ""))
        # Try to detect DR/CR from a separate indicator column.
        # Checks common naming variants including "Transaction Type" (Airtel bank).
        dc_col = (col_lower.get('debit_credit') or col_lower.get('dr_cr') or
                  col_lower.get('txn_type') or col_lower.get('type') or
                  col_lower.get('transaction type') or col_lower.get('transaction_type'))
        dr_cr = None
        if dc_col:
            dc_val = str(row.get(dc_col, '')).strip().upper()
            if dc_val in ('DR', 'DEBIT', 'D'):
                dr_cr = 'DR'
            elif dc_val in ('CR', 'CREDIT', 'C'):
                dr_cr = 'CR'
        return (val, dr_cr)

    # 2. Bank side: look for separate Debit / Credit amount columns
    if is_bank_side:
        debit_col  = next((col_lower[k] for k in ('debit', 'debits', 'debit amount', 'withdrawal', 'dr', 'dr amount', 'debit_amount') if k in col_lower), None)
        credit_col = next((col_lower[k] for k in ('credit', 'credits', 'credit amount', 'deposit', 'cr', 'cr amount', 'credit_amount') if k in col_lower), None)
        debit_val  = _safe_float(row.get(debit_col,  '')) if debit_col  else None
        credit_val = _safe_float(row.get(credit_col, '')) if credit_col else None
        if debit_val is not None and debit_val > 0:
            return (debit_val, 'DR')
        if credit_val is not None and credit_val > 0:
            return (credit_val, 'CR')
        # Negative debit = credit reversal (bank shows reversal as negative in Debits column)
        if debit_val is not None and debit_val < 0:
            return (abs(debit_val), 'CR')
        # Negative credit = debit (rare but handle gracefully)
        if credit_val is not None and credit_val < 0:
            return (abs(credit_val), 'DR')
        # Single amount column fallback (also handles Axis Bank's Amount(INR) column)
        amt_col = next((col_lower[k] for k in ('amount', 'txn amount', 'transaction amount', 'value', 'amount(inr)') if k in col_lower), None)
        if amt_col:
            raw_str = str(row.get(amt_col, '')).replace('\t', '').replace(',', '').strip()
            raw = _safe_float(raw_str)
            # Check for Debit/Credit indicator column (Axis Bank uses "Debit/Credit")
            dc_col2 = (col_lower.get('debit/credit') or col_lower.get('debit_credit') or
                       col_lower.get('dr_cr') or col_lower.get('transaction type'))
            dr_cr2 = None
            if dc_col2:
                dc_val2 = str(row.get(dc_col2, '')).strip().upper()
                if dc_val2 in ('DR', 'DEBIT', 'D'):
                    dr_cr2 = 'DR'
                elif dc_val2 in ('CR', 'CREDIT', 'C'):
                    dr_cr2 = 'CR'
            if raw is not None and raw < 0:
                return (abs(raw), 'CR')
            return (raw, dr_cr2)

    # 3. Internal side: find Amount + DEBIT_CREDIT
    else:
        dc_col  = next((col_lower[k] for k in ('debit_credit', 'dr_cr', 'txn_type', 'type', 'dr/cr') if k in col_lower), None)
        amt_col = next((col_lower[k] for k in ('amount', 'transferamt', 'transfer_amt', 'txn_amount',
                                                 'transaction_amount', 'debitamount', 'debit_amount') if k in col_lower), None)
        amt_val = _safe_float(row.get(amt_col, '')) if amt_col else None
        dr_cr = None
        if dc_col:
            dc_val = str(row.get(dc_col, '')).strip().upper()
            if dc_val in ('DR', 'DEBIT', 'D'):
                dr_cr = 'DR'
            elif dc_val in ('CR', 'CREDIT', 'C'):
                dr_cr = 'CR'
        # Negative amount overrides the DR/CR column (reversal)
        if amt_val is not None and amt_val < 0:
            return (abs(amt_val), 'CR' if dr_cr == 'DR' else dr_cr or 'CR')
        return (amt_val, dr_cr)

    return (None, None)


def _classify_bank_row(row: dict, df_columns: list) -> tuple:
    """
    Classify a bank-side row.
    Returns (row_type, recon_status) tuple:
      ("txn",               "unmatched")      — normal DMT debit transaction
      ("reversal",          "unmatched")      — IMPS/NEFT reversal — bank-to-bank pair needed
      ("fee_reversal",      "unmatched")      — fee charge reversal — bank-to-bank pair needed
      ("fee_charge",        "fee_matched")    — bank fee/IMPS charge — auto-closed
      ("fund_transfer",     "fund_transfer")  — fund transfer credit — auto-closed
      ("settlement_credit", "fund_transfer")  — RTGS/NEFT settlement inflow — auto-closed

    IMPORTANT: reversal check runs BEFORE fee_charge check so a reversed fee
    is classified as fee_reversal (needs pairing) and NOT auto-closed as fee_matched.
    """
    desc = ''
    # Try description-like column names first
    for col in df_columns:
        if 'desc' in col.lower() or 'narr' in col.lower() or 'remark' in col.lower():
            desc = str(row.get(col, ''))
            if desc:
                break
    # Fallback: scan all columns for known patterns
    if not desc:
        for col in df_columns:
            v = str(row.get(col, ''))
            if re.search(r'(?:IMPS|NEFT|RTGS|FEE\s*CHG|FUND\s*TRAN)', v, re.IGNORECASE):
                desc = v
                break

    # ── Reversal check (MUST come before fee_charge) ─────────────────────────
    # Two formats supported:
    #   Fino:   "FEE CHG IMPS Charges/607410029036-Reversal" → -Reversal suffix
    #   Airtel: "REV IMPS-7297413813-CBIN-..." → REV IMPS prefix
    # Both must be caught BEFORE fee_charge so reversed fees aren't auto-closed.
    is_reversal = (
        bool(re.search(r'-Reversal\b',  desc, re.IGNORECASE)) or  # Fino style
        bool(re.search(r'^REV\s+IMPS',  desc, re.IGNORECASE))     # Airtel style
    )
    if is_reversal:
        if re.search(r'FEE\s*CHG', desc, re.IGNORECASE):
            return ("fee_reversal", "unmatched")   # fee reversal — bank↔bank pairing
        return ("reversal", "unmatched")            # txn reversal — bank↔bank pairing

    # Bank fee/service charge rows (e.g. FEE CHG IMPS) — NOT a reversal
    if re.search(r'FEE\s*CHG', desc, re.IGNORECASE):
        return ("fee_charge", "fee_matched")

    # Airtel customer registration fees (UUID APB_TXN_id, no Partner_txn_id)
    if re.search(r'Customer\s+Registration\s+for\s+DMT', desc, re.IGNORECASE):
        return ("fee_charge", "fee_matched")

    # Fund transfer rows (e.g. FUND TRANSFER, FT/, TRF TO)
    if re.search(r'FUND\s*TRANS|FT\s*/|TRF\s*TO|TRANSFER\s*TO', desc, re.IGNORECASE):
        return ("fund_transfer", "fund_transfer")

    # Pure-credit rows via separate Debit/Credit columns (Fino PTA Bank style)
    # Fino bank statement has 'Debits' and 'Credits' columns.
    # If Credits > 0 and Debits == 0 → inbound settlement credit (not a DMT disbursement).
    # If Debits > 0 → normal DMT debit → falls through to txn below.
    debit_val = 0.0
    credit_val = 0.0
    has_dc_cols = False
    for col in df_columns:
        if 'debit' in col.lower() and 'credit' not in col.lower():
            has_dc_cols = True
            try:
                debit_val = float(str(row.get(col, '0')).replace(',', '') or '0')
            except Exception:
                pass
        if 'credit' in col.lower() and 'debit' not in col.lower():
            has_dc_cols = True
            try:
                credit_val = float(str(row.get(col, '0')).replace(',', '') or '0')
            except Exception:
                pass
    if has_dc_cols and credit_val > 0 and debit_val == 0:
        return ("settlement_credit", "fund_transfer")

    # Single DR/CR indicator column (Airtel-style: no separate debit/credit amount cols)
    # Check "Transaction Type", "DEBIT_CREDIT", "DR_CR", "TXN_TYPE", "Debit/Credit" (Axis).
    for col in df_columns:
        cl = col.strip().lower().replace(' ', '_')
        # Also match 'debit/credit' (Axis Bank) by normalising '/' → '_'
        cl_norm = cl.replace('/', '_')
        if cl_norm in ('transaction_type', 'debit_credit', 'dr_cr', 'txn_type'):
            cell_val = str(row.get(col, '')).strip().upper()
            if cell_val in ('CR', 'CREDIT', 'C'):
                return ("settlement_credit", "fund_transfer")
            break   # found the column — if it's DR, fall through to txn

    return ("txn", "unmatched")


def _extract_bank_account(original_filename: str, raw_path: str = None) -> Optional[str]:
    """Best-effort source bank-account number for a BANK statement.

    Order: (1) "Account No - <digits>" inside the first ~25 raw lines of the file
    (authoritative — every Axis statement carries it), then (2) a leading digit
    run in the filename (Axis exports are named by account number). Returns None
    when nothing plausible (9–18 digits) is found, so non-account files (e.g.
    SMB_RED_BK_<date>…) never get a bogus tag.
    """
    # 1) statement header
    if raw_path:
        try:
            with open(raw_path, "r", encoding="utf-8", errors="ignore") as fh:
                for _ in range(25):
                    line = fh.readline()
                    if not line:
                        break
                    m = re.search(r"[Aa]ccount\s*No[.\s:\-]*?(\d{9,18})", line)
                    if m:
                        return m.group(1)
        except Exception:
            pass
    # 2) filename must START with the account digits (avoids matching dates mid-name)
    if original_filename:
        m = re.match(r"\s*(\d{9,18})", os.path.basename(original_filename))
        if m:
            return m.group(1)
    return None


def _register_bank_account(partner: str, account_number: str, db: Session) -> None:
    """Upsert a discovered bank account into the registry (auto_added)."""
    if not account_number or not partner or partner == "mixed":
        return
    try:
        from models.database import BankAccount
        exists = db.query(BankAccount).filter(BankAccount.account_number == account_number).first()
        if not exists:
            db.add(BankAccount(
                partner=partner, account_number=account_number,
                label=f"{partner.title()} — A/c …{account_number[-4:]}",
                is_active=True, auto_added=True,
            ))
            db.commit()
    except Exception:
        db.rollback()


@router.post("/file")
async def upload_file(
    file: UploadFile = File(...),
    partner: str = Form(...),
    side: str = Form(...),
    recon_date: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("upload"))
):
    stored_name = f"{uuid.uuid4()}_{file.filename}"
    file_path = os.path.join(UPLOAD_DIR, stored_name)

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        df = load_file_to_dataframe(file_path, file.filename)
        # Strip leading/trailing whitespace from column names (common in exported dumps)
        df.columns = [c.strip() if isinstance(c, str) else c for c in df.columns]
        columns = list(df.columns)
        preview = df.head(5).fillna("").to_dict(orient="records")
        row_count = len(df)
    except Exception as e:
        os.remove(file_path)
        raise HTTPException(status_code=400, detail=f"Could not parse file: {str(e)}")

    # ── WLR — Wrong Record detection ─────────────────────────────────────────────
    # Checks if the file's column signature matches a DIFFERENT partner/format.
    # Hard-blocks the upload so wrong-account files never create silent open items.
    wlr_error = _check_wlr(partner, side, columns)
    if wlr_error:
        os.remove(file_path)
        raise HTTPException(status_code=422, detail=f"[WLR] {wlr_error}")

    # ── FREC — File Recognition Check ────────────────────────────────────────────
    # Reads a known cell/header to confirm the file belongs to the declared bank.
    # Configured per preset in BANK_FORMAT_PRESETS["frec"].
    frec_warning = None
    detected_preset = _detect_bank_format(partner, side, columns)
    if detected_preset:
        frec_error = _check_frec(df, detected_preset, partner, side)
        if frec_error:
            os.remove(file_path)
            raise HTTPException(status_code=422, detail=f"[FREC] {frec_error}")

    # ── Re-upload protection: check if data already exists for this slot ─────────
    existing_data = {}
    if recon_date != "auto":
        existing_count = db.query(Transaction).filter(
            Transaction.partner == partner,
            Transaction.side == side,
            Transaction.recon_date == recon_date,
            Transaction.row_type == "txn"
        ).count()
        if existing_count > 0:
            existing_data = {
                "count": existing_count,
                "partner": partner,
                "side": side,
                "date": recon_date
            }

    session = UploadSession(
        user_id=current_user.id,
        partner=partner,
        side=side,
        original_filename=file.filename,
        stored_filename=stored_name,
        recon_date=recon_date,
        status=UploadStatus.pending,
        row_count=row_count
    )
    db.add(session)
    db.commit()

    # Suggest saved template if exists
    saved_template = db.query(ColumnMappingTemplate).filter(
        ColumnMappingTemplate.partner == partner,
        ColumnMappingTemplate.side == side
    ).order_by(ColumnMappingTemplate.created_at.desc()).first()

    suggested_mapping = {}
    if saved_template:
        saved = json.loads(saved_template.mapping)
        for std_field, file_col in saved.items():
            if file_col in columns:
                suggested_mapping[std_field] = file_col

    # ── Bank format auto-detection ───────────────────────────────────────────
    format_detected = None
    preset = _detect_bank_format(partner, side, columns)
    if preset:
        # Preset takes priority: apply preset mapping directly
        col_map = {c.strip().lower(): c for c in columns}
        for std_field, file_col in preset["mapping"].items():
            if file_col.strip().lower() in col_map:
                suggested_mapping[std_field] = col_map[file_col.lower()]
        format_detected = preset["label"]

    # Smart defaults: auto-suggest mappings based on common column name patterns
    # Only runs when no preset was found (or fills in gaps preset didn't cover)
    if not suggested_mapping:
        col_lower = {c.strip().lower(): c for c in columns}
        guesses = {
            'eko_tid':          ['eko_trxn_id', 'tid', 'ekotid', 'eko_tid'],
            'tracking_number':  ['trackingnumber', 'tracking_number', 'tracking'],
            'utr_number':       ['utrnumber', 'utr_number', 'utr', 'utr_no'],
            # Note: 'debits' intentionally excluded — bank side uses two-column
            # (Debits + Credits) auto-detection in _auto_detect_amount, not a
            # single mapped column. Mapping a single debit col breaks CR row amounts.
            'amount':           ['amount', 'transferamt', 'transfer_amt', 'txn_amount',
                                  'debit amount', 'debit_amount', 'withdrawal'],
            'status':           ['status'],
            'transaction_date': ['transaction_date', 'txn_date', 'date', 'transaction effective date', 'value_date'],
            'description':      ['description', 'narration', 'remarks', 'desc'],
        }
        for std, candidates in guesses.items():
            for c in candidates:
                if c in col_lower:
                    suggested_mapping[std] = col_lower[c]
                    break

    return {
        "session_id":        session.id,
        "filename":          file.filename,
        "row_count":         row_count,
        "columns":           columns,
        "preview":           preview,
        "suggested_mapping": suggested_mapping,
        "standard_fields":   STANDARD_FIELDS,
        "format_detected":   format_detected,   # e.g. "Fino PTA Bank Statement" or null
        "existing_data":     existing_data,     # non-empty dict if data already exists for this slot
        "checks": {
            "wlr": "passed",    # only reached if WLR passed (would have 422'd otherwise)
            "frec": "passed",   # only reached if FREC passed (would have 422'd otherwise)
        },
    }


@router.post("/confirm-mapping")
def confirm_mapping(
    session_id: str = Form(...),
    mapping: str = Form(...),
    save_template: bool = Form(False),
    template_name: str = Form(""),
    filter_column: str = Form(""),      # e.g. "source"
    filter_value: str = Form(""),       # e.g. "FINO"
    source_column: str = Form(""),      # for mixed dump: column that holds FINO/AIRTEL/etc.
    force: bool = Form(False),          # admin-only override of the duplicate hard-block
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("upload"))
):
    session = db.query(UploadSession).filter(UploadSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Upload session not found")

    _ingest_t0 = time.monotonic()   # roadmap 1.4: ingestion-ledger duration timer

    # ── Hard-block re-upload (Re-upload/replace override) ────────────────────
    # Duplicate ingests caused a real double-counting incident (Jun 2026). For a fixed-date
    # slot that already has data, block ingestion unless force=true — which now means
    # REPLACE, not append: the slot's existing rows are recycle-binned and their matched
    # counterparts reverted before this file's rows land (see the force block below), so
    # the override is safe for any user with upload permission, not only admins.
    if session.recon_date and session.recon_date != "auto":
        _existing = db.query(Transaction).filter(
            Transaction.partner == session.partner,
            Transaction.side == session.side,
            Transaction.recon_date == session.recon_date,
        ).count()
        if _existing > 0:
            if not force:
                # roadmap 1.4: record the blocked re-upload attempt (isolated txn)
                try:
                    from core.ingestion_ledger import record_ingestion_event
                    record_ingestion_event(
                        channel="upload", status="blocked",
                        partner=session.partner, side=session.side,
                        recon_date=session.recon_date, username=current_user.username,
                        filename=session.original_filename, upload_session_id=session.id,
                        wlr_frec="passed",
                        duration_ms=int((time.monotonic() - _ingest_t0) * 1000),
                        detail={"reason": "duplicate_slot_block", "existing_rows": _existing},
                    )
                except Exception:
                    pass
                raise HTTPException(
                    status_code=409,
                    detail=(f"[DUPLICATE] {_existing} transactions already exist for "
                            f"{session.partner}/{session.side}/{session.recon_date}. "
                            "Use Re-upload (replace) to supersede them — the existing rows "
                            "go to the Recycle Bin and their matches are safely reverted, "
                            "so nothing is duplicated."),
                )

    mapping_dict: dict = json.loads(mapping)
    file_path = os.path.join(UPLOAD_DIR, session.stored_filename)

    try:
        df = load_file_to_dataframe(file_path, session.original_filename)
        # Strip leading/trailing whitespace from column names (common in exported dumps)
        df.columns = [c.strip() if isinstance(c, str) else c for c in df.columns]
        df = df.fillna("")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    df_columns = list(df.columns)
    is_bank_side = session.side == "bank"
    is_internal_side = session.side == "internal"
    is_auto_date = (session.recon_date == "auto")

    # ── Mixed-dump mode: resolve partner per row from source column ──────
    is_mixed = (session.partner == "mixed")
    # Build source→partner map from DB (includes Axis, Levin, and any admin-added partners)
    # Falls back to hardcoded map so existing uploads never break
    from core.ingest_service import _get_source_partner_map
    SOURCE_PARTNER_MAP = _get_source_partner_map(db)

    # ── TYPENAME filter: auto-detected from preset for any partner ───────────
    # Applies to partners whose internal dump preset declares a typename_filter
    # (aeps, pg, digikhata, indonepal when uploaded from the combined SMB dump).
    _typename_filter: Optional[str] = None
    if is_internal_side:
        _preset_internal = _detect_bank_format(session.partner, session.side, df_columns)
        if _preset_internal and "typename_filter" in _preset_internal:
            _typename_filter = _preset_internal["typename_filter"]

    # ── PG / QR bank: detect net_amount column from preset ────────────────
    # PG: "Net Amount" (post-gateway-fee amount)
    # QR: "Net Amount" (post-credit-card-fee amount, 0 for non-CC payers)
    _net_amount_col: Optional[str] = None
    if session.partner in ("pg", "qr") and is_bank_side:
        preset = _detect_bank_format(session.partner, session.side, df_columns)
        if preset and "net_amount_col" in preset:
            _net_amount_col = preset["net_amount_col"]

    # ── eko_tid prefix strip (Levin bank: Client_Ref_Id "EKOI…" → internal TID) ──
    # Detected once from the bank preset; applied per row during field mapping below.
    _eko_tid_strip_prefix: Optional[str] = None
    if is_bank_side:
        _preset_bank = _detect_bank_format(session.partner, session.side, df_columns)
        if _preset_bank and _preset_bank.get("eko_tid_strip_prefix"):
            _eko_tid_strip_prefix = _preset_bank["eko_tid_strip_prefix"]

    # ── AS=SD Integrity Checks ────────────────────────────────────────────────
    # Item 1: PayU per-row arithmetic check (warning only, does NOT block ingestion)
    # Item 2: AePS Fingpay — update cross-file PIIntegrityCheck state for this date
    integrity_warnings = []

    if session.partner == "pg" and is_bank_side:
        payu_check = _check_as_sd_payu(df, df_columns)
        if payu_check and not payu_check["passed"]:
            integrity_warnings.append(payu_check)

    if session.partner == "aeps" and is_bank_side:
        # AePS Fingpay cw-success-failure file: compute sum of Success Transaction Amounts
        _status_col = next(
            (c for c in df_columns if c.strip().lower() == "status message"), None
        )
        _amount_col = next(
            (c for c in df_columns if c.strip().lower() == "transaction amount"), None
        )
        if _status_col and _amount_col:
            def _sf(v):
                try: return float(str(v).replace(",", "").strip() or 0)
                except Exception: return 0.0
            _success_rows = df[df[_status_col].str.strip().str.lower() == "success"]
            _cw_sum   = round(sum(_sf(v) for v in _success_rows[_amount_col]), 2)
            _cw_count = len(_success_rows)
            _ic = _update_aeps_integrity_cw(db, session.recon_date, _cw_sum, _cw_count)
            if _ic and _ic.get("status") == "failed":
                integrity_warnings.append({
                    "check":   "AS=SD",
                    "partner": "aeps",
                    **_ic,
                })
            elif _ic and _ic.get("status") == "pending_tplus":
                integrity_warnings.append({
                    "check":   "AS=SD",
                    "partner": "aeps",
                    "status":  "pending_tplus",
                    "message": _ic.get("message", ""),
                    "cw_amount": _cw_sum,
                    "cw_row_count": _cw_count,
                })

    txns = []
    skipped = 0  # rows excluded due to unknown source or TYPENAME mismatch only

    # ── Fino internal-dump exclusion ────────────────────────────────────────
    # Fino's internal dump carries ACCOUNT_ACTION_ID (col L). Action-id 118 =
    # failed / booking-reversal entries that must NEVER be reconciled or reported.
    # Drop them at ingest so the rule is enforced once, everywhere downstream.
    # Fino-only: in a mixed Axis+Fino dump, Axis action-id 118 rows are kept.
    _fino_action_col = next(
        (c for c in df_columns if c.strip().upper() == 'ACCOUNT_ACTION_ID'), None
    ) if is_internal_side else None

    # Source bank-account number (bank side only) — tag each row so a partner with
    # multiple accounts (e.g. Axis) can be filtered/reported per account.
    _bank_acct = _extract_bank_account(
        session.original_filename, os.path.join(UPLOAD_DIR, session.stored_filename)
    ) if is_bank_side else None

    # ── Funds-position capture (bank side; reporting only, never matched on) ──
    # Detect the statement's running-balance column once: name contains 'balance'
    # but isn't an opening/closing header column.
    _balance_col = None
    if is_bank_side:
        for _c in df_columns:
            _cl = _c.strip().lower()
            if "balance" in _cl and "opening" not in _cl and "closing" not in _cl:
                _balance_col = _c
                break
    _funds_lines = {}   # (partner, bank_acct) → [line dicts in file order]

    for _, row in df.iterrows():
        row_dict = dict(row)

        # ── Mixed dump: resolve partner from source column ─────────────────
        if is_mixed:
            src_val = str(row_dict.get(source_column, '')).strip().upper() if source_column else ''
            row_partner = SOURCE_PARTNER_MAP.get(src_val)
            if not row_partner:
                skipped += 1   # unknown source (e.g. Axis) — skip
                continue
        else:
            row_partner = session.partner
            # Legacy single-partner filter (e.g. source = FINO)
            if filter_column and filter_value:
                cell = str(row_dict.get(filter_column, '')).strip()
                if cell and cell.upper() not in NULL_VALUES and cell.upper() != filter_value.strip().upper():
                    skipped += 1
                    continue

        # ── Fino: drop ACCOUNT_ACTION_ID == 118 (failed/reversal booking) ──
        if _fino_action_col and row_partner == 'fino':
            _aid = str(row_dict.get(_fino_action_col, '')).strip().strip('"').split('.')[0]
            if _aid == '118':
                skipped += 1
                continue

        # ── AePS / PG dump: filter by TYPENAME ────────────────────────────
        if _typename_filter:
            typename_col = next(
                (c for c in df_columns if c.strip().lower() == 'typename'), None
            )
            if typename_col:
                row_typename = str(row_dict.get(typename_col, '')).strip()
                if row_typename != _typename_filter:
                    skipped += 1
                    continue

        # ── CR rows: NEVER drop any entry (data integrity policy) ────────────
        # Reversal credit entries (Refunded/Failed CR) are real money movements
        # that must be visible in Open Items. The FAILED_STATUSES check below
        # correctly sets recon_status="failed" for Refunded rows.
        # (No CR filter applied — all rows pass through)

        # ── Classify bank-side rows ────────────────────────────────────────
        row_type = "txn"
        auto_recon_status = "unmatched"

        if is_bank_side:
            if session.partner == "digikhata":
                # Digikhata: detect eKYC Charge rows (appear in bank only, no internal match)
                _preset_dk = _detect_bank_format(session.partner, session.side, df_columns) or {}
                _kyc_col   = _preset_dk.get("kyc_product_col", "Product")
                _kyc_val   = _preset_dk.get("kyc_product_val", "eKYC Charge")
                _prod_col  = next((c for c in df_columns if c.strip() == _kyc_col), None)
                if _prod_col and str(row_dict.get(_prod_col, '')).strip() == _kyc_val:
                    row_type          = "kyc_charge"
                    auto_recon_status = "fee_matched"  # bank-only charge, no internal match needed

            elif session.partner == "indonepal":
                # Indonepal format always has:
                #   Row 1: "Transaction Report" title        → stripped by load_file_to_dataframe (header detection)
                #   Row 2: DATEFROM=... filter string        → stripped by load_file_to_dataframe (header detection)
                #   Row 3: actual column headers             → becomes DataFrame columns
                #   Row N: "Total" summary row               → must be skipped every time
                #
                # Skip any row that is NOT a real transaction:
                #   Condition A — Sno is not a positive integer  (catches "Total", blank, header repeats)
                #   Condition B — PartnerPinNo is blank/null     (catches Total + any stray empty rows)
                _sno_col = next((c for c in df_columns if c.strip() == "Sno"), None)
                _pin_col = next((c for c in df_columns if c.strip() == "PartnerPinNo"), None)
                _sno_ok  = False
                if _sno_col:
                    try:
                        _sno_ok = int(float(str(row_dict.get(_sno_col, '')).strip())) > 0
                    except (ValueError, TypeError):
                        _sno_ok = False
                _pin_val = str(row_dict.get(_pin_col, '')).strip() if _pin_col else ''
                _pin_ok  = bool(_pin_val) and _pin_val not in NULL_VALUES
                if not _sno_ok or not _pin_ok:
                    skipped += 1
                    continue   # Total / metadata / empty row — not a real transaction

            elif session.partner == "qr":
                # QR: pure transaction report — no fee/fund classification needed.
                # Failed rows (Txn Status in FAILED_STATUSES) are classified below.
                pass  # row_type stays "txn"; status check below handles failed

            elif session.partner not in ("aeps", "pg"):
                # DMT partners: full fee/fund classification
                row_type, auto_recon_status = _classify_bank_row(row_dict, df_columns)
        # fee_charge/kyc_charge → fee_matched; fund_transfer → fund_transfer
        # Only "txn" rows are unmatched and participate in reconciliation.

        # ── Direct field mapping ──────────────────────────────────────────
        eko_tid      = _clean(row_dict.get(mapping_dict.get("eko_tid", ""), ""))
        # Levin bank: strip the "EKOI" prefix so Client_Ref_Id matches the internal
        # dump's eko_trxn_id (preset-driven via eko_tid_strip_prefix).
        if _eko_tid_strip_prefix and eko_tid and eko_tid[:len(_eko_tid_strip_prefix)].upper() == _eko_tid_strip_prefix.upper():
            eko_tid = eko_tid[len(_eko_tid_strip_prefix):]
        tracking_num = _clean(row_dict.get(mapping_dict.get("tracking_number", ""), ""))
        utr_num      = _clean(row_dict.get(mapping_dict.get("utr_number", ""), ""))
        status       = _clean(row_dict.get(mapping_dict.get("status", ""), ""))
        txn_date     = _clean(row_dict.get(mapping_dict.get("transaction_date", ""), ""))

        # ── Detect failed transactions (internal dump only) ───────────────
        # Rows with a failure status are stored but excluded from matching.
        # They show in Open Items as "failed" (orange badge) for visibility.
        FAILED_STATUSES = {
            'failed', 'failure', 'fail', 'f', '0',
            'rejected', 'reversed', 'cancelled', 'canceled',
            'error', 'expired', 'declined', 'refunded', 'timeout',
        }
        if is_internal_side and row_type == "txn" and status and status.lower() in FAILED_STATUSES:
            auto_recon_status = "failed"
        # QR bank exports (Paypoint) carry a per-order Status; a Failed order moved
        # no money, so it must never sit as unmatched (or match a retried Success
        # row sharing the same order id). QR ONLY — other products' bank statements
        # don't carry a per-row status this way (DEFAULT_RULES qr note).
        if (is_bank_side and row_type == "txn" and row_partner == "qr"
                and status and status.lower() in FAILED_STATUSES):
            auto_recon_status = "failed"

        # ── Auto-date mode: derive recon_date from this row's transaction date ─
        if is_auto_date:
            row_recon_date = _parse_recon_date(txn_date) if txn_date else session.recon_date
        else:
            row_recon_date = session.recon_date

        # ── Auto-detect amount + DR/CR ────────────────────────────────────
        mapped_amt_col = mapping_dict.get("amount", "")
        amount, dr_cr  = _auto_detect_amount(row_dict, df_columns, mapped_amt_col, is_bank_side)

        # AePS cashout bank statement: a "Withdrawal" transaction type = money out → DR.
        if session.partner == "aeps" and is_bank_side and not dr_cr:
            if any("withdrawal" in str(v).lower() for v in row_dict.values()):
                dr_cr = "DR"

        # Levin bank statement rows are all outgoing DMT payouts → DR.
        if session.partner == "levin" and is_bank_side and amount and not dr_cr:
            dr_cr = "DR"

        # ── Auto-extract TID + Tracking from description field ────────────
        desc_col = mapping_dict.get("description", "")
        desc_val = str(row_dict.get(desc_col, "")).strip() if desc_col else ""

        # Also scan all columns for IMPS/NEFT/RTGS pattern if not mapped
        if not desc_val:
            for col_val in row_dict.values():
                v = str(col_val)
                if re.search(r'(?:IMPS|NEFT|RTGS)/', v, re.IGNORECASE):
                    desc_val = v
                    break

        if desc_val:
            parsed = _parse_description(desc_val)
            if not eko_tid and parsed["eko_tid"]:
                eko_tid = parsed["eko_tid"]
            if not tracking_num and parsed["tracking_number"]:
                tracking_num = parsed["tracking_number"]
            if not utr_num and parsed["utr_number"]:
                utr_num = parsed["utr_number"]

        # ── PG bank: extract net_amount (post-fee settlement amount) ─────
        net_amount = None
        if _net_amount_col:
            net_amount = _safe_float(str(row_dict.get(_net_amount_col, '')).replace(',', ''))

        # ── CSP (retailer) identity — internal dump only (read-only, additive) ─
        _csp_code, _csp_name = _extract_csp(row_dict) if is_internal_side else ("", "")

        # ── Funds-position: running balance + notification-row classification ──
        # (bank side only; reporting-only — matching logic untouched)
        _row_balance = None
        if is_bank_side:
            if _balance_col:
                _row_balance = _safe_float(str(row_dict.get(_balance_col, '')).replace(',', ''))
            _desc_u = (desc_val or "").strip().upper()
            # Notification lines (OPENING/CLOSING BALANCE, statement footers/legends)
            # carry NO money movement and NO identifiers. They are kept in the ledger
            # (director decision: retain the whole statement) but classified out of
            # reconciliation as auto-closed 'notification' rows. Deliberately narrow:
            # a real ₹0 txn with a DR/CR marker or any identifier still ingests as txn.
            if (row_type == "txn" and not dr_cr and (amount is None or amount == 0)
                    and not (eko_tid or tracking_num or utr_num)):
                row_type = "notification"
                auto_recon_status = "fund_transfer"   # existing auto-closed status
            # Collect the funds line (file order) for the EOD balance snapshot.
            _dr = _cr = 0.0
            if amount:
                if dr_cr == "DR":
                    _dr = amount
                elif dr_cr == "CR":
                    _cr = amount
                elif row_type in ("settlement_credit", "fund_transfer", "reversal"):
                    _cr = amount
                elif row_type != "notification":
                    _dr = amount            # DMT payouts/fees default to debit
            _funds_lines.setdefault((row_partner, _bank_acct or ""), []).append({
                "date": row_recon_date, "balance": _row_balance, "dr": _dr, "cr": _cr,
                "stated_opening": _row_balance if _desc_u == "OPENING BALANCE" else None,
                "stated_closing": _row_balance if _desc_u == "CLOSING BALANCE" else None,
            })

        txn = Transaction(
            id=generate_id(),
            upload_session_id=session.id,
            partner=row_partner,
            side=session.side,
            recon_date=row_recon_date,
            eko_tid=eko_tid,
            tracking_number=tracking_num,
            utr_number=utr_num,
            amount=amount,
            net_amount=net_amount,
            dr_cr=dr_cr,
            status=status,
            transaction_date=txn_date,
            row_type=row_type,
            recon_status=auto_recon_status,
            bank_account=_bank_acct,
            raw_data=json.dumps(row_dict),
            # Store the original bank narration for display (bank side only;
            # internal-dump rows keep NULL). Empty string (not NULL) for bank
            # rows with no narration, so the startup backfill's IS NULL filter
            # only ever touches pre-existing rows. Read-only — not used in matching.
            bank_description=(desc_val or "") if is_bank_side else None,
            # CSP (retailer) code + name for display/search — internal side only;
            # bank rows keep NULL. "" (not NULL) when the dump carries no CSP
            # column, so the startup backfill's IS NULL filter only ever touches
            # pre-existing rows. Read-only — never used in matching.
            csp_code=(_csp_code if is_internal_side else None),
            csp_name=(_csp_name if is_internal_side else None),
            # Running statement balance (bank side; display/funds-position only)
            balance=(_row_balance if is_bank_side else None),
        )
        txns.append(txn)

    # ── force = REPLACE, not append ────────────────────────────────────────────
    # A forced re-upload supersedes this file's OWN scope: every (partner, side,
    # recon_date) triple its parsed rows cover — nothing outside the file is touched.
    # Existing rows in those scopes are recycle-binned (recoverable) and their matched
    # counterparts reverted first (same cascade as /upload/clear), so re-applying a
    # file can never double-count and never leaves a stale match.
    replaced_rows = 0
    if force and txns:
        from core import recycle_bin as _rb
        _scopes = {(t.partner, t.side, t.recon_date) for t in txns
                   if t.partner and t.side and t.recon_date}
        _rep_batch = generate_id()
        for _p, _s, _d in sorted(_scopes):
            q_old = db.query(Transaction).filter(Transaction.partner == _p,
                                                 Transaction.side == _s,
                                                 Transaction.recon_date == _d)
            old_ids = [t.id for t in q_old.with_entities(Transaction.id).all()]
            if not old_ids:
                continue
            db.query(Transaction).filter(Transaction.matched_with_id.in_(old_ids)).update(
                {"recon_status": "unmatched", "matched_with_id": None, "match_id": None},
                synchronize_session=False)
            _evibt = {m for (m,) in db.query(Transaction.match_id)
                      .filter(Transaction.id.in_(old_ids),
                              Transaction.match_id.like("EVIBT-%")).all() if m}
            if _evibt:
                from models.database import EvalueBankTxn as _EvB2
                db.query(_EvB2).filter(_EvB2.match_id.in_(_evibt)).update(
                    {"recon_status": "unmatched_bank", "match_id": None,
                     "match_note": "core interbank leg replaced by re-upload — auto-reverted"},
                    synchronize_session=False)
            replaced_rows += _rb.soft_delete(
                db, q_old, Transaction, module="core", user=current_user.username,
                filters={"partner": _p, "side": _s, "recon_date": _d},
                reason="re-upload (replace)", batch_id=_rep_batch)

    db.bulk_save_objects(txns)
    session.status = UploadStatus.done
    session.column_mapping = mapping
    session.row_count = len([t for t in txns if t.row_type == "txn"])
    db.commit()

    # Auto-register a newly seen bank account so it appears in the registry.
    if _bank_acct and is_bank_side and session.partner not in (None, "", "mixed"):
        _register_bank_account(session.partner, _bank_acct, db)

    # ── Funds-position: upsert per-date EOD balance snapshots (never blocks) ──
    if is_bank_side and _funds_lines:
        try:
            from core.funds import record_snapshots
            from models.database import PartnerConfig as _PC
            for (_fp, _facct), _flines in _funds_lines.items():
                _pc = db.query(_PC).filter(_PC.slug == _fp).first()
                _prod = (_pc.product if _pc and _pc.product else "dmt")
                record_snapshots(db, _prod, _fp, _facct, _flines,
                                 uploaded_by=current_user.username)
        except Exception as _e:
            logger.warning(f"funds snapshot skipped: {_e}")

    # ── Reversal matching: run first, before auto-recon ─────────────────────────
    # Pairs bank reversal rows (row_type reversal/fee_reversal) with their originals
    # using tracking_number across all dates. Runs on bank uploads only.
    reversal_results = {}
    if is_bank_side:
        from core.matching_engine import run_reversal_match
        bank_pairs = set((t.partner, t.recon_date) for t in txns
                         if t.row_type in ("reversal", "fee_reversal") and t.recon_date)
        for (p, d) in bank_pairs:
            if p and d:
                try:
                    rev_r = run_reversal_match(p, d, db, current_user.id)
                    if rev_r["reversal_matched"] > 0:
                        reversal_results[f"{p}/{d}"] = rev_r
                except Exception as _e:
                    logger.warning(f"routes/upload.py: {_e}")  # reversal errors don't block the upload response

    # ── Auto-recon: runs after BOTH bank AND internal uploads ────────────────────
    # Bidirectional: uploading the bank triggers matching against existing internal rows,
    # and vice versa. This ensures correct matching regardless of upload order.
    auto_recon_results = {}
    from core.matching_engine import run_reconciliation
    _recon_pairs = set((t.partner, t.recon_date) for t in txns if t.row_type == "txn" and t.recon_date)

    # Run auto-recon for EVERY uploaded pair where the counterpart side already exists
    # (bank triggers recon if internal is already there; internal triggers if bank is already there)
    counterpart_side = "bank" if is_internal_side else "internal"
    for (p, d) in _recon_pairs:
        if p and d:
            # Check counterpart exists before running recon
            counterpart_count = db.query(Transaction).filter(
                Transaction.partner == p,
                Transaction.recon_date == d,
                Transaction.side == counterpart_side,
                Transaction.row_type == "txn",
            ).limit(1).count()
            if counterpart_count == 0:
                continue   # counterpart not uploaded yet — skip for now
            try:
                r = run_reconciliation(p, d, db, current_user.id)
                total_bank = r.get("total_bank", 0) or r.get("unmatched_bank", 0) + r.get("matched", 0)
                match_rate = round(r["matched"] / max(total_bank, 1) * 100, 1) if total_bank else 0
                auto_recon_results[f"{p}/{d}"] = {
                    "matched":            r["matched"],
                    "unmatched_bank":     r["unmatched_bank"],
                    "unmatched_internal": r["unmatched_internal"],
                    "match_rate":         match_rate,
                    "alert":              match_rate < 85,   # flag low match rate
                }
            except Exception as _e:
                logger.warning(f"routes/upload.py: {_e}")  # recon errors don't block the upload response

    # ── Auto NEFT D+1 after internal dump upload ─────────────────────────────────
    # For each date that has just been imported, try to match yesterday's open
    # bank NEFT rows (UTR-based) against today's new internal rows.
    neft_d1_results = {}
    if is_internal_side:
        from core.matching_engine import run_neft_d1_match
        for (p, d) in _recon_pairs:
            if p and d:
                try:
                    neft_r = run_neft_d1_match(p, d, db, current_user.id)
                    if neft_r["neft_d1_matched"] > 0:
                        neft_d1_results[f"{p}/{d}"] = neft_r
                except Exception as _e:
                    logger.warning(f"routes/upload.py: {_e}")  # NEFT D+1 errors don't block the upload response

    # ── Cross-date RRN pass (QR T+1) — bank credit on D, internal record on D+1 ──
    # Per-(partner, recon_date) matching never sees those pairs; this identifier-only
    # pass (RRN + amount ≤ ₹1, across dates) closes them. Bidirectional (either side
    # may arrive last) and scoped to CROSS_DATE_RRN_PARTNERS only.
    cross_date_results = {}
    try:
        from core.matching_engine import run_cross_date_rrn_match, CROSS_DATE_RRN_PARTNERS
        for p in set(p for (p, _d) in _recon_pairs if p in CROSS_DATE_RRN_PARTNERS):
            xr = run_cross_date_rrn_match(p, db, current_user.id)
            if xr.get("cross_date_rrn_matched", 0) > 0:
                cross_date_results[p] = xr
    except Exception as _e:
        logger.warning(f"routes/upload.py: {_e}")  # cross-date errors don't block the upload response

    # ── Internal self-match after internal dump upload ───────────────────────────
    # Nets off DR/CR contra pairs (and Success+Refund) WITHIN the internal dump so
    # reversed/cancelled entries don't linger as failed/unmatched open items.
    # Runs after auto-recon + NEFT so anything that matched the bank is left alone.
    # Applies to every product (keyed per partner/date — handles mixed dumps too).
    internal_match_results = {}
    if is_internal_side:
        from core.matching_engine import run_internal_match
        for (p, d) in _recon_pairs:
            if p and d:
                try:
                    im = run_internal_match(p, d, db, current_user.id)
                    if im.get("internal_matched", 0) > 0:
                        internal_match_results[f"{p}/{d}"] = im
                except Exception as _e:
                    logger.warning(f"routes/upload.py: {_e}")  # internal-match errors don't block the upload response

    # ── Same-side duplicate flagging (re-ingested rows) ──────────────────────────
    # Overlapping/repeated uploads — or a re-upload after a clear wiped the
    # file-hash guard — re-insert the same transaction. Flag those redundant copies
    # as 'duplicate' so they drop out of Open Items; the matched/first copy is kept.
    # txn rows only — reversals legitimately share tracking numbers (contract #18).
    duplicate_results = {}
    try:
        from core.matching_engine import flag_same_side_duplicates
        for p in set(t.partner for t in txns if t.row_type == "txn" and t.partner):
            dr = flag_same_side_duplicates(p, session.side, db)
            if dr["duplicates_flagged"] > 0:
                duplicate_results[f"{p}/{session.side}"] = dr
    except Exception as _e:
        logger.warning(f"routes/upload.py: {_e}")  # dedup errors don't block the upload response

    if save_template and template_name:
        existing = db.query(ColumnMappingTemplate).filter(
            ColumnMappingTemplate.partner == session.partner,
            ColumnMappingTemplate.side == session.side,
            ColumnMappingTemplate.name == template_name
        ).first()
        if existing:
            existing.mapping = mapping
        else:
            tmpl = ColumnMappingTemplate(
                name=template_name,
                partner=session.partner,
                side=session.side,
                mapping=mapping,
                created_by=current_user.id
            )
            db.add(tmpl)
        db.commit()

    txn_count      = len([t for t in txns if t.row_type == "txn"])
    fee_count      = len([t for t in txns if t.row_type == "fee_charge"])
    cred_count     = len([t for t in txns if t.row_type == "settlement_credit"])
    reversal_count = len([t for t in txns if t.row_type in ("reversal", "fee_reversal")])

    # Per-partner breakdown (useful for mixed dump)
    partner_counts = {}
    for t in txns:
        if t.row_type == "txn":
            partner_counts[t.partner] = partner_counts.get(t.partner, 0) + 1

    fund_count = len([t for t in txns if t.row_type in ("fund_transfer", "settlement_credit")])

    # ── Record upload history ─────────────────────────────────────────────────
    display_date = "auto (multi-date)" if is_auto_date else session.recon_date
    try:
        db.add(UploadHistory(
            user_id         = current_user.id,
            username        = current_user.username,
            filename        = session.original_filename,
            partner         = session.partner,
            side            = session.side,
            recon_date      = display_date,
            rows_inserted   = txn_count,
            format_detected = session.column_mapping,   # store raw mapping json; label resolved on read if needed
        ))
        db.commit()
    except Exception:
        db.rollback()

    # ── Audit log ─────────────────────────────────────────────────────────────
    try:
        db.add(AuditLog(
            user_id     = current_user.id,
            username    = current_user.username,
            action      = "upload",
            action_type = "human",
            entity_type = "upload_session",
            entity_id   = session.id,
            detail      = json.dumps({
                "filename": session.original_filename,
                "partner":  session.partner,
                "side":     session.side,
                "recon_date": display_date,
                "rows_inserted": txn_count,
                "partner_counts": partner_counts,
            }),
        ))
        db.commit()
    except Exception:
        db.rollback()

    # ── SEV notice: check if settlement bank is configured for this partner ──────
    sev_notice = None
    if is_bank_side and session.partner in ("pg", "aeps", "qr"):
        try:
            sev_cfg = db.query(SettlementBankConfig).filter(
                SettlementBankConfig.partner == session.partner,
                SettlementBankConfig.is_active == True
            ).first()
            if not sev_cfg:
                sev_notice = (
                    f"Settlement bank not configured for '{session.partner}' — SEV skipped. "
                    f"Add a settlement bank account in Configuration → Settlement Banks to enable "
                    f"two-stage settlement matching."
                )
        except Exception as _e:
            logger.warning(f"sev_cfg lookup: {_e}")

    # ── Pre-ingest data-quality profile (roadmap 1.6 — read-only, never gates) ──
    _dq = None
    try:
        from core.data_quality import profile_dataframe
        _dq = profile_dataframe(df, mapping_dict)
    except Exception:
        _dq = None

    # ── Ingestion lineage ledger (roadmap 1.4 — additive, isolated txn) ──────────
    # Reads counters already computed above; never alters the ingest result.
    try:
        from core.ingestion_ledger import record_ingestion_event, sha256_of_file
        record_ingestion_event(
            channel="upload", status="completed",
            partner=session.partner, side=session.side,
            recon_date=display_date, username=current_user.username,
            filename=session.original_filename,
            file_sha256=sha256_of_file(file_path),
            file_size=(os.path.getsize(file_path) if os.path.exists(file_path) else None),
            preset_detected=(_detect_bank_format(session.partner, session.side, df_columns) or {}).get("label"),
            rows_read=int(len(df)),
            rows_accepted=(txn_count + fee_count + cred_count + reversal_count),
            rows_skipped=skipped,
            wlr_frec="passed",   # WLR/FREC enforced at /upload/file before this point
            duration_ms=int((time.monotonic() - _ingest_t0) * 1000),
            upload_session_id=session.id,
            dq_profile=_dq,
        )
    except Exception:
        pass

    return {
        # roadmap 1.6: read-only data-quality profile (new Step-3 field, additive #25)
        "data_quality": _dq,
        "message": "Ingested successfully",
        "replaced_rows": replaced_rows,   # force=true: prior rows superseded (recycle-binned)
        "row_count": txn_count,
        "fee_charge_count": fee_count,
        "settlement_credit_count": cred_count,
        "fund_transfer_count": fund_count,
        "reversal_count": reversal_count,
        "partner_counts": partner_counts,
        "skipped": skipped,
        "session_id": session.id,
        "auto_recon": auto_recon_results if is_internal_side else {},
        "neft_d1_auto": neft_d1_results if is_internal_side else {},
        "cross_date_auto": cross_date_results,
        "reversal_auto": reversal_results if is_bank_side else {},
        # Re-ingested rows flagged as 'duplicate' (per partner/side), if any
        "duplicate_flagged": duplicate_results,
        # AS=SD integrity warnings (non-blocking)
        "integrity_warnings": integrity_warnings,
        # SEV notice when settlement bank not yet configured
        "sev_notice": sev_notice,
    }


@router.get("/history")
def get_upload_history(
    partner: Optional[str] = None,
    side: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Return paginated upload history — most recent first."""
    q = db.query(UploadHistory)
    if partner: q = q.filter(UploadHistory.partner == partner)
    if side:    q = q.filter(UploadHistory.side == side)
    total = q.count()
    rows  = q.order_by(UploadHistory.uploaded_at.desc()) \
             .offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "items": [
            {
                "id":              h.id,
                "username":        h.username,
                "filename":        h.filename,
                "partner":         h.partner,
                "side":            h.side,
                "recon_date":      h.recon_date,
                "rows_inserted":   h.rows_inserted,
                "uploaded_at":     str(h.uploaded_at),
            }
            for h in rows
        ]
    }


@router.get("/sessions")
def list_sessions(
    partner: Optional[str] = None,
    recon_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    q = db.query(UploadSession)
    if partner: q = q.filter(UploadSession.partner == partner)
    if recon_date: q = q.filter(UploadSession.recon_date == recon_date)
    sessions = q.order_by(UploadSession.upload_date.desc()).limit(100).all()
    return [{"id": s.id, "partner": s.partner, "side": s.side, "filename": s.original_filename,
             "recon_date": s.recon_date, "row_count": s.row_count, "status": s.status,
             "upload_date": str(s.upload_date)} for s in sessions]


@router.get("/templates")
def list_templates(partner: Optional[str] = None, side: Optional[str] = None,
                   db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    q = db.query(ColumnMappingTemplate)
    if partner: q = q.filter(ColumnMappingTemplate.partner == partner)
    if side: q = q.filter(ColumnMappingTemplate.side == side)
    templates = q.all()
    return [{"id": t.id, "name": t.name, "partner": t.partner, "side": t.side,
             "mapping": json.loads(t.mapping)} for t in templates]


def _module_tables(module: str):
    """(model, date_expr, side) for every data table of a module.

    date_expr → how a `recon_date` filter scopes THIS table (each has its own business-date
                column); None = the table has no business date and can never be date-scoped.
    side      → 'bank' | 'internal', or None for tables that aren't one side of a pair
                (master/config/derived results).
    """
    from models.database import (
        BbpsBankTxn, BbpsInternal, EvalueBankTxn, EvalueWalletLoad,
        SBIBankTransaction, SBITxnReport, SBIKOLimits, SBIKOCashHolding,
        SBILimitFailure, SBICSPMaster, SBIP01Result, SBIP02Result,
        SBIP03Result, SBIP04Result,
    )
    from sqlalchemy import func as _F

    def col(name):
        return lambda M: getattr(M, name)

    # An E-Value load reconciles on its VALUE date (behavior-contract item 13).
    def ev_load_date(M):
        return _F.coalesce(_F.nullif(M.value_date, ""), M.transaction_date)

    return {
        "bbps": [
            (BbpsBankTxn,        col("transaction_date"), "bank"),
            (BbpsInternal,       col("transaction_date"), "internal"),
        ],
        "evalue": [                                        # EvalueAccount = config, kept
            (EvalueBankTxn,      col("txn_date"),         "bank"),
            (EvalueWalletLoad,   ev_load_date,            "internal"),
        ],
        "sbi": [
            (SBIBankTransaction, col("txn_date"),         "bank"),
            (SBITxnReport,       col("txn_date"),         "internal"),
            (SBIKOLimits,        col("txn_date"),         None),
            (SBIKOCashHolding,   col("report_date"),      None),
            (SBILimitFailure,    col("txn_date"),         None),
            (SBICSPMaster,       None,                    None),   # master data — no business date
            (SBIP01Result,       col("recon_date"),       None),
            (SBIP02Result,       col("recon_date"),       None),
            (SBIP03Result,       col("recon_date"),       None),
            (SBIP04Result,       col("recon_date"),       None),
        ],
    }.get(module, [])


def _clear_module(module: str, db, *, recon_date=None, side=None,
                  date_from=None, date_to=None, dry_run=False,
                  user="system", filters=None, batch_id=None) -> dict:
    """Delete a module's data rows, HONOURING the caller's filters, via the recycle bin.

    This function previously ignored recon_date/side entirely and wiped every table of the
    product. A user asking to clear ONE date on the bank side therefore lost the whole
    product — bank rows, transaction reports, KO limits, cash holding, limit failures AND
    the P01-P04 results (live incident, 2026-07-17). Now:
      • recon_date scopes each table by its OWN business date;
      • side restricts to that side's table;
      • a table that cannot honour a supplied filter is SKIPPED, never wiped wholesale;
      • every deleted row goes to the recycle bin first, so a mistake is recoverable.
    """
    from core import recycle_bin
    batch_id = batch_id or generate_id()
    deleted = {}
    # Pair tables whose rows carry (recon_status, match_id) linking legs of a match:
    # (model, unmatched-literal, the FULL set of statuses that represent a linked pair —
    # NOT a 'matched%' prefix: wrong_amount / twice_credit / amount_mismatch /
    # failed_refunded etc. are linked pairs too and must also revert).
    from models.database import BbpsBankTxn, BbpsInternal, EvalueBankTxn, EvalueWalletLoad
    _EV_PAIRED = ("matched_online", "matched_cash", "matched_manual", "wrong_amount",
                  "twice_credit", "interbank_matched")
    _BBPS_PAIRED = ("matched", "amount_mismatch", "failed_refunded",
                    "failed_pending_refund", "refunded_but_success")
    _PAIR_STATUS = {
        "evalue": [(EvalueBankTxn, "unmatched_bank", _EV_PAIRED),
                   (EvalueWalletLoad, "unmatched_load", _EV_PAIRED)],
        "bbps":   [(BbpsBankTxn, "unmatched_bank", _BBPS_PAIRED),
                   (BbpsInternal, "unmatched_internal", _BBPS_PAIRED)],
    }
    _pair_models = {m for m, _, _ in _PAIR_STATUS.get(module, [])}
    _deleted_mids = set()                 # match_ids whose rows are being deleted
    _sbi_source_deleted = False

    for model, date_expr, tside in _module_tables(module):
        if side and tside != side:
            continue                      # not this side's table → leave it alone
        q = db.query(model)
        if recon_date:
            if date_expr is None:
                continue                  # no business date → refuse to wipe it
            q = q.filter(date_expr(model) == recon_date)
        elif date_from or date_to:
            if date_expr is None:
                continue                  # can't range-scope a dateless table → skip, never wipe
            if date_from: q = q.filter(date_expr(model) >= date_from)
            if date_to:   q = q.filter(date_expr(model) <= date_to)
        if model in _pair_models:
            _deleted_mids.update(m for (m,) in q.with_entities(model.match_id)
                                 .filter(model.match_id.isnot(None)).all())
        if dry_run:
            n = q.count()                 # PREVIEW only — count, touch nothing
        else:
            n = recycle_bin.soft_delete(db, q, model, module=module, user=user,
                                        filters=filters, reason=f"clear {module}",
                                        batch_id=batch_id)
        if n:
            deleted[model.__tablename__] = n
            if module == "sbi" and model.__tablename__ in ("sbi_bank_transactions",
                                                           "sbi_txn_reports"):
                _sbi_source_deleted = True

    # ── Match-integrity cascade ────────────────────────────────────────────────
    # Deleting one leg of a matched pair must revert every SURVIVING leg sharing its
    # match_id to unmatched. Auto matches self-heal on the next run, but manual /
    # preserved dispositions (EVMAN-/EVX-/EVIBT-) survive re-runs by design — without
    # this they stay "matched" forever, pointing at deleted rows (live incident: five
    # stale matched_manual wallet loads after a bank-side clear, 2026-07-22).
    cascaded = 0
    if _deleted_mids and dry_run:
        # preview of how many surviving legs WOULD revert
        for pm, unmatched_status, paired in _PAIR_STATUS.get(module, []):
            cascaded += (db.query(pm)
                         .filter(pm.match_id.in_(_deleted_mids),
                                 pm.recon_status.in_(paired)).count())
        _evibt = {m for m in _deleted_mids if m and str(m).startswith("EVIBT-")}
        if _evibt:
            cascaded += db.query(Transaction).filter(Transaction.match_id.in_(_evibt)).count()
    elif _deleted_mids:
        for pm, unmatched_status, paired in _PAIR_STATUS.get(module, []):
            cascaded += (db.query(pm)
                         .filter(pm.match_id.in_(_deleted_mids),
                                 pm.recon_status.in_(paired))
                         .update({"recon_status": unmatched_status, "match_id": None,
                                  "match_note": "partner leg deleted by clear — auto-reverted"},
                                 synchronize_session=False))
        # Cross-product interbank links: an EVIBT- match pairs an EvalueBankTxn with a
        # CORE ledger Transaction (which carries the match_id but NO matched_with_id, so
        # the core clear's own cascade can't see it). Deleting the E-Value leg must
        # reset the core leg too — mirror of the /evalue/unmatch cross-table reset.
        _evibt = {m for m in _deleted_mids if m and str(m).startswith("EVIBT-")}
        if _evibt:
            cascaded += (db.query(Transaction)
                         .filter(Transaction.match_id.in_(_evibt))
                         .update({"recon_status": "unmatched", "match_id": None,
                                  "matched_with_id": None},
                                 synchronize_session=False))

    # ── SBI: source rows gone ⇒ that scope's results are fiction ───────────────
    # P01-P04 result rows claim Matched with soft-FKs into the source tables; once the
    # sources for a date are cleared they are unrecomputable (the wipe-guard rightly
    # refuses to re-run an empty date) and would lie forever. Delete the same scope's
    # results (recycle-binned, same batch) whenever a source side was cleared under a
    # side filter (with no side filter the main loop already removed them).
    if module == "sbi" and _sbi_source_deleted and side:
        from models.database import (SBIP01Result as _P1, SBIP02Result as _P2,
                                     SBIP03Result as _P3, SBIP04Result as _P4)
        for rm in (_P1, _P2, _P3, _P4):
            q = db.query(rm)
            if recon_date:
                q = q.filter(rm.recon_date == recon_date)
            elif date_from or date_to:
                if date_from: q = q.filter(rm.recon_date >= date_from)
                if date_to:   q = q.filter(rm.recon_date <= date_to)
            if dry_run:
                n = q.count()
            else:
                n = recycle_bin.soft_delete(db, q, rm, module=module, user=user,
                                            filters=filters,
                                            reason=f"clear sbi ({side} side cleared — results stale)",
                                            batch_id=batch_id)
            if n:
                deleted[rm.__tablename__] = deleted.get(rm.__tablename__, 0) + n

    # The upload-hash guard only blocks re-uploading an identical FILE. Dropping it is
    # correct ONLY for a full, unfiltered product clear (and never during a dry run).
    if not recon_date and not side and not date_from and not date_to and not dry_run:
        from models.database import ModuleUploadHash
        db.query(ModuleUploadHash).filter(
            ModuleUploadHash.module == module).delete(synchronize_session=False)

    return {"deleted": deleted, "total": sum(deleted.values()),
            "counterparts_unmatched": cascaded, "batch_id": batch_id,
            "dry_run": dry_run}


# product slug → module key (kiosk and sbi both map to the SBI tables)
_CLEAR_MODULE_OF = {"bbps": "bbps", "evalue": "evalue", "sbi": "sbi", "kiosk": "sbi"}


@router.delete("/clear")
def clear_data(
    partner: Optional[str] = None,
    recon_date: Optional[str] = None,
    side: Optional[str] = None,          # "bank" | "internal"
    recon_status: Optional[str] = None,  # "matched" | "unmatched" | "src_assigned" | "all"
    date_from: Optional[str] = None,     # inclusive range (used when recon_date is empty)
    date_to: Optional[str] = None,       # inclusive range
    dry_run: bool = False,               # preview: exact counts, deletes NOTHING
    db: Session = Depends(get_db),
    # Destructive — gated by the 'clear_data' permission ("Clear / Delete Data" in
    # the Users screen). Admins short-circuit; non-admins need the toggle granted.
    # (Previously hard-coded to admin-only, which ignored the clear_data permission
    # that the UI/model already expose and admins assign — a wiring bug.)
    current_user: User = Depends(require_permission("clear_data"))
):
    # ── Module products live in their own tables ──────────────────────────────
    if partner in _CLEAR_MODULE_OF:
        module = _CLEAR_MODULE_OF[partner]
        # A status filter can't be honoured across a module's tables — each product has its
        # own status vocabulary and the P0x results use match_status. REFUSE rather than
        # silently ignore it and delete more than the user asked for.
        if recon_status and recon_status != "all":
            raise HTTPException(
                status_code=400,
                detail=(f"Clearing '{partner}' by status isn't supported — this product keeps "
                        f"its own tables with their own status vocabulary. Clear by date "
                        f"and/or side, or act on specific rows from the {partner} screen."))
        _filters = {k: v for k, v in {"partner": partner, "recon_date": recon_date,
                                      "side": side, "date_from": date_from,
                                      "date_to": date_to}.items() if v}
        res = _clear_module(module, db, recon_date=recon_date, side=side,
                            date_from=date_from, date_to=date_to, dry_run=dry_run,
                            user=current_user.username, filters=_filters)
        if dry_run:
            return {"dry_run": True, "would_delete": res["total"], "by_table": res["deleted"],
                    "counterparts_would_unmatch": res["counterparts_unmatched"],
                    "module": partner, "filters_applied": _filters}
        try:
            db.add(AuditLog(user_id=current_user.id, username=current_user.username,
                            action="clear", action_type="human", entity_type="module",
                            detail=json.dumps({"module": partner, "deleted_rows": res["total"],
                                               "by_table": res["deleted"], "filters": _filters,
                                               "recycle_batch_id": res["batch_id"]})))
        except Exception:
            pass
        db.commit()
        return {"deleted_transactions": res["total"], "deleted_sessions": 0, "module": partner,
                "by_table": res["deleted"], "recycle_batch_id": res["batch_id"],
                "counterparts_unmatched": res["counterparts_unmatched"],
                "filters_applied": _filters}
    """
    Clear transactions (and orphaned upload sessions) with granular filters.
    All filters are optional and combinable:
      partner      → fino / airtel
      recon_date   → YYYY-MM-DD
      side         → bank / internal
      recon_status → matched / unmatched / src_assigned / all (omit = all)
    """
    q_txn = db.query(Transaction)
    if partner:      q_txn = q_txn.filter(Transaction.partner == partner)
    if recon_date:   q_txn = q_txn.filter(Transaction.recon_date == recon_date)
    elif date_from or date_to:
        # inclusive business-date range (zero-padded strings compare lexicographically)
        if date_from: q_txn = q_txn.filter(Transaction.recon_date >= date_from)
        if date_to:   q_txn = q_txn.filter(Transaction.recon_date <= date_to)
    if side:         q_txn = q_txn.filter(Transaction.side == side)
    if recon_status and recon_status != "all":
        q_txn = q_txn.filter(Transaction.recon_status == recon_status)

    # ── Dry run: exact counts for the confirmation preview — deletes NOTHING ──
    if dry_run:
        _ids = [t.id for t in q_txn.with_entities(Transaction.id).all()]
        _cp = (db.query(Transaction)
               .filter(Transaction.matched_with_id.in_(_ids)).count()) if _ids else 0
        _ev = (db.query(Transaction.match_id)
               .filter(Transaction.id.in_(_ids),
                       Transaction.match_id.like("EVIBT-%")).count()) if _ids else 0
        return {"dry_run": True, "would_delete": len(_ids),
                "counterparts_would_unmatch": _cp + _ev,
                "filters_applied": {k: v for k, v in {
                    "partner": partner, "recon_date": recon_date, "side": side,
                    "recon_status": recon_status, "date_from": date_from,
                    "date_to": date_to}.items() if v}}

    # ── Cascade: reset counterpart matches before deleting ────────────────────
    # Collect IDs of rows about to be deleted, then find any transaction on the
    # OTHER side that was matched against one of them. Reset those counterparts
    # to 'unmatched' so they don't remain stale-matched with a dangling reference.
    ids_to_delete = [t.id for t in q_txn.with_entities(Transaction.id).all()]
    affected_pairs = set()   # (partner, recon_date) pairs whose counterparts were reset
    if ids_to_delete:
        # Capture (partner, date) of counterparts BEFORE resetting — needed for auto-recon
        counterpart_rows = (
            db.query(Transaction.partner, Transaction.recon_date)
            .filter(
                Transaction.matched_with_id.in_(ids_to_delete),
                Transaction.recon_date.isnot(None),
                Transaction.partner.isnot(None),
            )
            .distinct()
            .all()
        )
        affected_pairs = {(r.partner, r.recon_date) for r in counterpart_rows}

        counterparts_reset = (
            db.query(Transaction)
            .filter(Transaction.matched_with_id.in_(ids_to_delete))
            .update(
                {"recon_status": "unmatched", "matched_with_id": None, "match_id": None},
                synchronize_session=False,
            )
        )
        # Cross-product interbank links: an EVIBT- match pairs a core Transaction with an
        # EvalueBankTxn. The core leg carries the match_id but NOT matched_with_id (the
        # partner lives in another table), so the reset above can't see the E-Value leg.
        # Collect the deleted rows' EVIBT- mids and reset those E-Value bank rows too.
        _evibt_mids = {m for (m,) in db.query(Transaction.match_id)
                       .filter(Transaction.id.in_(ids_to_delete),
                               Transaction.match_id.like("EVIBT-%")).all() if m}
        if _evibt_mids:
            from models.database import EvalueBankTxn as _EvB
            counterparts_reset += (db.query(_EvB)
                                   .filter(_EvB.match_id.in_(_evibt_mids))
                                   .update({"recon_status": "unmatched_bank", "match_id": None,
                                            "match_note": "core interbank leg deleted by clear — auto-reverted"},
                                           synchronize_session=False))
    else:
        counterparts_reset = 0

    # Capture into the recycle bin BEFORE deleting, so a mis-scoped core clear is
    # recoverable too (not just module clears).
    from core import recycle_bin
    _batch = generate_id()
    deleted_txn = recycle_bin.soft_delete(
        db, q_txn, Transaction, module="core", user=current_user.username,
        filters={k: v for k, v in {"partner": partner, "recon_date": recon_date,
                                   "side": side, "recon_status": recon_status}.items() if v},
        reason="clear core ledger", batch_id=_batch)

    # Clean up upload sessions that no longer have any transactions
    from sqlalchemy import text
    orphans = db.execute(text(
        "SELECT id FROM upload_sessions WHERE id NOT IN "
        "(SELECT DISTINCT upload_session_id FROM transactions WHERE upload_session_id IS NOT NULL)"
    )).fetchall()
    orphan_ids = [r[0] for r in orphans]
    deleted_ses = 0
    if orphan_ids:
        deleted_ses = db.query(UploadSession).filter(
            UploadSession.id.in_(orphan_ids)
        ).delete(synchronize_session=False)

    db.commit()

    # ── Auto-recon: re-run matching for pairs whose counterparts were just reset ─
    # Only fires when BOTH sides still have unmatched rows for a given (partner, date).
    # If the deleted side had no surviving counterpart data, this is a no-op.
    auto_recon_results = {}
    if affected_pairs:
        from core.matching_engine import run_reconciliation
        for (p, d) in sorted(affected_pairs):
            try:
                has_bank = db.query(Transaction.id).filter(
                    Transaction.partner == p, Transaction.recon_date == d,
                    Transaction.side == "bank", Transaction.recon_status == "unmatched",
                    Transaction.row_type == "txn",
                ).first()
                has_dump = db.query(Transaction.id).filter(
                    Transaction.partner == p, Transaction.recon_date == d,
                    Transaction.side == "internal", Transaction.recon_status == "unmatched",
                    Transaction.row_type == "txn",
                ).first()
                if has_bank and has_dump:
                    r = run_reconciliation(p, d, db, current_user.id)
                    auto_recon_results[f"{p}/{d}"] = r.get("matched", 0)
            except Exception as _e:
                logger.warning(f"routes/upload.py: {_e}")  # Audit log
    try:
        db.add(AuditLog(
            user_id=current_user.id, username=current_user.username,
            action="clear", action_type="human", entity_type="transaction",
            detail=json.dumps({
                "deleted_transactions": deleted_txn,
                "counterparts_reset": counterparts_reset,
                "auto_recon_pairs": auto_recon_results,
                "filters": {k: v for k, v in {
                    "partner": partner, "recon_date": recon_date,
                    "side": side, "recon_status": recon_status}.items() if v}
            }),
        ))
        db.commit()
    except Exception:
        db.rollback()

    # ── Clear-everything also wipes the dedicated module tables ───────────────
    # Only when no scoping filter is set (true "clear all" intent).
    module_deleted = 0
    if not partner and not recon_date and not side and recon_status in (None, "", "all"):
        for _m in ("bbps", "evalue", "sbi"):
            module_deleted += _clear_module(_m, db, user=current_user.username,
                                            filters={"clear_all": True},
                                            batch_id=_batch)["total"]
        db.commit()

    return {
        "deleted_transactions": deleted_txn + module_deleted,
        "deleted_sessions": deleted_ses,
        "deleted_module_rows": module_deleted,
        "recycle_batch_id": _batch,
        "filters_applied": {
            k: v for k, v in {"partner": partner, "recon_date": recon_date,
                               "side": side, "recon_status": recon_status}.items() if v
        }
    }


@router.delete("/clear-selected")
def clear_selected(
    transaction_ids: str,   # comma-separated list of IDs
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a specific set of transactions by ID (for checkbox bulk-delete in UI)."""
    ids = [i.strip() for i in transaction_ids.split(",") if i.strip()]
    if not ids:
        return {"deleted_transactions": 0}

    # Maker-checker: non-admin deletes queue for approval when enabled
    from core import maker_checker
    queued = maker_checker.intercept(
        db, current_user, "delete_txns",
        payload={"transaction_ids": transaction_ids},
        summary=f"Delete {len(ids)} transaction(s)",
    )
    if queued:
        return queued

    # Cascade: reset counterparts before deleting (incl. cross-product EVIBT- legs)
    db.query(Transaction).filter(
        Transaction.matched_with_id.in_(ids)
    ).update(
        {"recon_status": "unmatched", "matched_with_id": None, "match_id": None},
        synchronize_session=False,
    )
    _evibt_sel = {m for (m,) in db.query(Transaction.match_id)
                  .filter(Transaction.id.in_(ids),
                          Transaction.match_id.like("EVIBT-%")).all() if m}
    if _evibt_sel:
        from models.database import EvalueBankTxn as _EvBSel
        db.query(_EvBSel).filter(_EvBSel.match_id.in_(_evibt_sel)).update(
            {"recon_status": "unmatched_bank", "match_id": None,
             "match_note": "core interbank leg deleted — auto-reverted"},
            synchronize_session=False)

    # Soft delete via the recycle bin (was a hard delete — checkbox bulk-deletes must be
    # recoverable like every other financial-row deletion).
    from core import recycle_bin as _rb_sel
    deleted = _rb_sel.soft_delete(
        db, db.query(Transaction).filter(Transaction.id.in_(ids)), Transaction,
        module="core", user=current_user.username,
        filters={"ids_count": len(ids)}, reason="delete selected rows")
    db.commit()

    # Audit log
    try:
        db.add(AuditLog(
            user_id=current_user.id, username=current_user.username,
            action="delete_selected", action_type="human", entity_type="transaction",
            detail=json.dumps({"deleted": deleted, "ids_count": len(ids)}),
        ))
        db.commit()
    except Exception:
        db.rollback()

    return {"deleted_transactions": deleted}


@router.post("/delete-rows")
def delete_rows(
    module: str = Form(...),             # core | evalue | bbps | sbi
    table: str = Form(...),              # core: txn · evalue/bbps: bank|internal · sbi: bank|txn
    row_ids: str = Form(...),            # comma-separated ids
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("clear_data")),
):
    """Row-level multi-select delete for ANY window — recycle-binned, match-cascaded.

    The per-window checkbox delete (Mismatches / product pages / All Entries) routes
    here. Every deletion: (1) goes to the recycle bin (one restorable batch), (2)
    reverts every surviving matched counterpart sharing the deleted rows' match ids
    (full paired-status sets, incl. cross-product EVIBT- legs), and (3) for SBI source
    rows, re-runs the affected business dates so the P0x results stay truthful."""
    ids = [i.strip() for i in row_ids.split(",") if i.strip()]
    if not ids:
        return {"deleted": 0}
    from core import recycle_bin
    from models.database import (BbpsBankTxn, BbpsInternal, EvalueBankTxn,
                                 EvalueWalletLoad, SBIBankTransaction, SBITxnReport)
    _EV_PAIRED = ("matched_online", "matched_cash", "matched_manual", "wrong_amount",
                  "twice_credit", "interbank_matched")
    _BBPS_PAIRED = ("matched", "amount_mismatch", "failed_refunded",
                    "failed_pending_refund", "refunded_but_success")
    REG = {
        ("core", "txn"):       (Transaction, None, None, None),
        ("evalue", "bank"):    (EvalueBankTxn, EvalueWalletLoad, "unmatched_load", _EV_PAIRED),
        ("evalue", "internal"):(EvalueWalletLoad, EvalueBankTxn, "unmatched_bank", _EV_PAIRED),
        ("bbps", "bank"):      (BbpsBankTxn, BbpsInternal, "unmatched_internal", _BBPS_PAIRED),
        ("bbps", "internal"):  (BbpsInternal, BbpsBankTxn, "unmatched_bank", _BBPS_PAIRED),
        ("sbi", "bank"):       (SBIBankTransaction, None, None, None),
        ("sbi", "txn"):        (SBITxnReport, None, None, None),
    }
    key = (module, table)
    if key not in REG:
        raise HTTPException(status_code=400, detail=f"Unknown module/table: {module}/{table}")
    model, partner_model, partner_unmatched, paired = REG[key]

    q = db.query(model).filter(model.id.in_(ids))
    cascaded = 0
    sbi_dates = set()
    if module == "core":
        # same cascade as clear-selected (matched_with_id + EVIBT- cross-product)
        cascaded = db.query(Transaction).filter(Transaction.matched_with_id.in_(ids)).update(
            {"recon_status": "unmatched", "matched_with_id": None, "match_id": None},
            synchronize_session=False)
        _ev = {m for (m,) in db.query(Transaction.match_id)
               .filter(Transaction.id.in_(ids), Transaction.match_id.like("EVIBT-%")).all() if m}
        if _ev:
            cascaded += db.query(EvalueBankTxn).filter(EvalueBankTxn.match_id.in_(_ev)).update(
                {"recon_status": "unmatched_bank", "match_id": None,
                 "match_note": "core interbank leg deleted — auto-reverted"},
                synchronize_session=False)
    elif partner_model is not None:
        mids = {m for (m,) in q.with_entities(model.match_id)
                .filter(model.match_id.isnot(None)).all()}
        if mids:
            # partner table + same-table siblings (EVX- links two bank rows) + EVIBT core legs
            for pm, unm in ((partner_model, partner_unmatched),
                            (model, "unmatched_bank" if table == "bank" else
                             ("unmatched_load" if module == "evalue" else "unmatched_internal"))):
                cascaded += (db.query(pm)
                             .filter(pm.match_id.in_(mids), pm.recon_status.in_(paired),
                                     ~pm.id.in_(ids))
                             .update({"recon_status": unm, "match_id": None,
                                      "match_note": "partner row deleted — auto-reverted"},
                                     synchronize_session=False))
            _ev = {m for m in mids if str(m).startswith("EVIBT-")}
            if _ev:
                cascaded += db.query(Transaction).filter(Transaction.match_id.in_(_ev)).update(
                    {"recon_status": "unmatched", "match_id": None, "matched_with_id": None},
                    synchronize_session=False)
    elif module == "sbi":
        datecol = SBIBankTransaction.txn_date if table == "bank" else SBITxnReport.txn_date
        sbi_dates = {d for (d,) in q.with_entities(datecol).distinct().all() if d}

    deleted = recycle_bin.soft_delete(
        db, q, model, module=module, user=current_user.username,
        filters={"table": table, "ids_count": len(ids)}, reason="delete selected rows")
    db.commit()

    sbi_rerun = []
    if sbi_dates:
        # regenerate the affected dates' results so they never point at deleted rows
        try:
            from routes.sbi_kiosk import _run_all_dates
            _run_all_dates(db, current_user, sorted(sbi_dates))
            sbi_rerun = sorted(sbi_dates)
        except Exception as _e:
            logger.warning(f"delete-rows sbi re-run failed: {_e}")

    try:
        db.add(AuditLog(user_id=current_user.id, username=current_user.username,
                        action="delete_rows", action_type="human", entity_type=f"{module}:{table}",
                        detail=json.dumps({"deleted": deleted, "counterparts_unmatched": cascaded,
                                           "sbi_rerun_dates": sbi_rerun})))
        db.commit()
    except Exception:
        db.rollback()
    return {"deleted": deleted, "counterparts_unmatched": cascaded,
            "sbi_rerun_dates": sbi_rerun}


def _parse_recon_date(date_str: str) -> Optional[str]:
    """
    Parse a date string from a transaction row and return YYYY-MM-DD.
    Handles common formats: 2026-04-15, 15-04-2026, 15/04/2026,
    '2026-04-15 06:06:36' (timestamp), '18 Jun 2026 11:59 PM' (QR Paypoint
    ExportTransactionList export), etc. Returns None if parsing fails.
    """
    if not date_str:
        return None
    import datetime as dt
    s = date_str.strip()
    # Formats whose DATE TEXT itself contains spaces (e.g. the QR Paypoint
    # "ExportTransactionList" export's "18 Jun 2026 11:59 PM") must be matched
    # against the full string BEFORE any time-stripping: splitting on the first
    # space would wrongly truncate the day off the month/year and yield NULL.
    full_formats = [
        "%d %b %Y %I:%M %p",   # 18 Jun 2026 11:59 PM
        "%d %b %Y %H:%M:%S",   # 18 Jun 2026 23:59:00
        "%d %b %Y %H:%M",      # 18 Jun 2026 23:59
        "%d %b %Y",            # 18 Jun 2026
        "%d-%b-%Y %I:%M %p",   # 18-Jun-2026 11:59 PM
    ]
    for fmt in full_formats:
        try:
            return dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Date text has no internal spaces — strip any trailing time component and
    # match the date-only formats (unchanged from the original behaviour).
    date_only = s.split(" ")[0].split("T")[0]
    formats = [
        "%Y-%m-%d",   # 2026-04-15
        "%d-%m-%Y",   # 15-04-2026
        "%d/%m/%Y",   # 15/04/2026
        "%Y/%m/%d",   # 2026/04/15
        "%m/%d/%Y",   # 04/15/2026
        "%d-%b-%Y",   # 15-Apr-2026
        "%d %b %Y",   # 15 Apr 2026
    ]
    for fmt in formats:
        try:
            return dt.datetime.strptime(date_only, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _safe_float(val) -> Optional[float]:
    try:
        cleaned = str(val).replace(",", "").strip()
        return float(cleaned) if cleaned and cleaned not in NULL_VALUES else None
    except Exception:
        return None
