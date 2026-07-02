"""
core/ingest_service.py

Shared ingestion logic used by both the HTTP upload route (routes/upload.py)
and the auto-upload service (routes/auto_upload.py).

`ingest_dataframe()` is the core function — it takes a pandas DataFrame +
config and writes transactions to the DB, then optionally runs reversal
matching, auto-recon, and NEFT D+1.
"""

import os
import json
import re
import datetime
import shutil
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from models.database import (
    UploadSession, Transaction, UploadStatus, generate_id,
    ColumnMappingTemplate, AuditLog, UploadHistory
)

# ── Re-export constants/helpers so auto_upload doesn't need to import from routes ──
# (These are defined in routes/upload.py and imported here to avoid circular deps)
NULL_VALUES = {'\\N', 'NULL', 'null', 'None', 'none', 'NA', 'N/A', ''}
UPLOAD_DIR  = os.path.join(os.path.dirname(__file__), "..", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DATE_FORMAT_MAP = {
    "YYYYMMDD":   "%Y%m%d",
    "YYYY-MM-DD": "%Y-%m-%d",
    "DDMMYYYY":   "%d%m%Y",
    "DD-MM-YYYY": "%d-%m-%Y",
    "DD/MM/YYYY": "%d/%m/%Y",
}

# ── Source partner map (for mixed dumps) ───────────────────────────────────────
SOURCE_PARTNER_MAP = {
    'FINO':   'fino',   'AIRTEL':  'airtel',
    'fino':   'fino',   'airtel':  'airtel',
    'Axis':   'axis',   'AXIS':    'axis',   'axis':  'axis',
    'AXIS_UPI': 'axis',  'Axis_UPI': 'axis',  'axis_upi': 'axis',
    'Levin':  'levin',  'LEVIN':   'levin',  'levin': 'levin',
    # QR Collection
    'QR Collection':          'qr',
    'qr collection':          'qr',
    'QR COLLECTION':          'qr',
    # PPI Wallet Load
    'Digi Khata Load Wallet': 'digikhata',
    'digi khata load wallet': 'digikhata',
    'DIGI KHATA LOAD WALLET': 'digikhata',
    # Indonepal Money Transfer
    'Indo-Nepal': 'indonepal',
    'indo-nepal': 'indonepal',
    'INDO-NEPAL': 'indonepal',
    'Indo Nepal': 'indonepal',
}

def _get_source_partner_map(db: Session) -> dict:
    """
    Build the source→partner map from the hardcoded dict + any active PartnerConfig
    rows in the DB that have a source_value set. DB rows supplement (don't override)
    so new admin-added partners are picked up automatically.
    """
    result = dict(SOURCE_PARTNER_MAP)
    try:
        from models.database import PartnerConfig
        for p in db.query(PartnerConfig).filter(
            PartnerConfig.is_active == True,
            PartnerConfig.source_value.isnot(None)
        ).all():
            sv = p.source_value
            # Add lowercase, uppercase, and title-case variants
            for variant in (sv, sv.upper(), sv.lower(), sv.title()):
                result.setdefault(variant, p.slug)
    except Exception:
        pass
    return result


FAILED_STATUSES = {
    'failed', 'failure', 'fail', 'f', '0',
    'rejected', 'reversed', 'cancelled', 'canceled',
    'error', 'expired', 'declined', 'refunded', 'timeout',
}


# ─── File pattern helpers ─────────────────────────────────────────────────────

def build_expected_filename(config, target_date: datetime.date = None) -> str:
    """Build the expected filename for a given date (defaults to today)."""
    if target_date is None:
        target_date = datetime.date.today()
    fmt = DATE_FORMAT_MAP.get(config.date_format, "%Y%m%d")
    date_str = target_date.strftime(fmt)
    return f"{config.file_prefix or ''}{date_str}{config.file_suffix or '.xlsx'}"


# All file extensions that are acceptable as bank statement / dump files.
# When the configured suffix is not found, we try these alternates automatically.
_FALLBACK_EXTENSIONS = [".xlsx", ".xls", ".csv", ".pdf"]


def detect_file_for_today(config, target_date: datetime.date = None) -> tuple:
    """
    Locate today's file in the configured folder.
    Returns (full_path, expected_filename).
    full_path is None if the file does not exist.

    Extension fallback:
    - First tries the exact configured suffix (e.g. .xlsx).
    - If not found, tries the same base name with .xlsx / .xls / .csv / .pdf
      so that placing a PDF in a folder configured for .xlsx still works.
    """
    if not config.folder_path:
        return None, "(no folder configured)"

    primary_name = build_expected_filename(config, target_date)
    primary_path = os.path.join(config.folder_path, primary_name)
    if os.path.exists(primary_path):
        return primary_path, primary_name

    # Try alternate extensions using the same base name (prefix + date)
    fmt      = DATE_FORMAT_MAP.get(config.date_format, "%Y%m%d")
    dt       = target_date or datetime.date.today()
    date_str = dt.strftime(fmt)
    base     = f"{config.file_prefix or ''}{date_str}"
    configured_ext = (config.file_suffix or ".xlsx").lower()

    for ext in _FALLBACK_EXTENSIONS:
        if ext == configured_ext:
            continue  # already tried above
        candidate = base + ext
        candidate_path = os.path.join(config.folder_path, candidate)
        if os.path.exists(candidate_path):
            return candidate_path, candidate  # return actual filename found

    return None, primary_name  # not found; return primary name for error messages


# ─── Core ingestion ───────────────────────────────────────────────────────────

def ingest_dataframe(
    df,
    session: UploadSession,
    mapping_dict: dict,
    db: Session,
    user_id: str,
    username: str,
    filter_column: str = "",
    filter_value:  str = "",
    source_column: str = "",
    do_auto_recon: bool = True,
) -> dict:
    """
    Transactional wrapper around the ingestion pipeline: any failure rolls back
    partially-inserted rows and marks the upload session failed, so a crashed
    ingest can never leave half a file in the transactions table.
    """
    import time as _time
    _t0 = _time.monotonic()
    # roadmap 1.4: the auto/watch-folder channel (least-supervised) — WLR/FREC and
    # slot/hash guards are NOT enforced here (known issue R2), so record that fact.
    _channel = "watch_folder" if username in ("auto-upload", "openclaw-bot") else "auto"
    try:
        result = _ingest_dataframe_inner(
            df, session, mapping_dict, db, user_id, username,
            filter_column, filter_value, source_column, do_auto_recon,
        )
    except Exception as _exc:
        db.rollback()
        try:
            session.status = UploadStatus.failed
            db.add(session)
            db.commit()
        except Exception:
            db.rollback()
        try:
            from core.ingestion_ledger import record_ingestion_event
            record_ingestion_event(
                channel=_channel, status="failed",
                partner=session.partner, side=session.side,
                recon_date=session.recon_date, username=username,
                filename=session.original_filename, upload_session_id=session.id,
                wlr_frec="not_checked",
                duration_ms=int((_time.monotonic() - _t0) * 1000),
                detail={"error": str(_exc)[:500]},
            )
        except Exception:
            pass
        raise

    # ── Ingestion lineage ledger (roadmap 1.4 — additive, isolated txn) ──────────
    try:
        from core.ingestion_ledger import record_ingestion_event, sha256_of_file
        _sha = _size = None
        try:
            from routes.upload import UPLOAD_DIR
            _fp = os.path.join(UPLOAD_DIR, session.stored_filename or "")
            if session.stored_filename and os.path.exists(_fp):
                _sha, _size = sha256_of_file(_fp), os.path.getsize(_fp)
        except Exception:
            pass
        _accepted = (result.get("row_count", 0) + result.get("fee_charge_count", 0)
                     + result.get("settlement_credit_count", 0) + result.get("reversal_count", 0))
        _dq = None
        try:
            from core.data_quality import profile_dataframe
            _dq = profile_dataframe(df, mapping_dict)
        except Exception:
            _dq = None
        record_ingestion_event(
            channel=_channel, status="completed",
            partner=session.partner, side=session.side,
            recon_date=session.recon_date, username=username,
            filename=session.original_filename,
            file_sha256=_sha, file_size=_size,
            rows_read=int(len(df)),
            rows_accepted=_accepted,
            rows_skipped=result.get("skipped"),
            wlr_frec="not_checked",
            duration_ms=int((_time.monotonic() - _t0) * 1000),
            upload_session_id=session.id,
            dq_profile=_dq,
        )
    except Exception:
        pass
    return result


def _ingest_dataframe_inner(
    df,
    session: UploadSession,
    mapping_dict: dict,
    db: Session,
    user_id: str,
    username: str,
    filter_column: str = "",
    filter_value:  str = "",
    source_column: str = "",
    do_auto_recon: bool = True,
) -> dict:
    """
    Process a pandas DataFrame through the full ingestion pipeline:
    1.  Row iteration + classification + field extraction
    2.  Bulk insert to transactions table
    3.  Reversal matching (bank side)
    4.  Auto-recon (internal side)
    5.  NEFT D+1 matching (internal side)
    6.  Upload history record
    7.  Audit log record

    Returns the same payload shape as the confirm-mapping HTTP endpoint.
    """
    # Import helpers from routes/upload to avoid code duplication
    from routes.upload import (
        _classify_bank_row, _auto_detect_amount, _parse_description,
        _clean, _safe_float, _parse_recon_date,
        _extract_bank_account, _register_bank_account, _extract_csp, UPLOAD_DIR,
    )
    import os as _os

    df.columns = [c.strip() if isinstance(c, str) else c for c in df.columns]
    df = df.fillna("")
    df_columns = list(df.columns)

    is_bank_side     = session.side == "bank"
    is_internal_side = session.side == "internal"
    is_auto_date     = (session.recon_date == "auto")
    is_mixed         = (session.partner == "mixed")

    # Build source→partner map once per ingest call (DB supplemented)
    _src_map = _get_source_partner_map(db)

    txns    = []
    skipped = 0

    # ── Fino internal-dump exclusion (mirrors routes/upload.py) ─────────────
    # Action-id 118 in the Fino internal dump = failed / booking-reversal entries
    # that must never be reconciled or reported. Dropped at ingest, Fino-only.
    _fino_action_col = next(
        (c for c in df_columns if c.strip().upper() == 'ACCOUNT_ACTION_ID'), None
    ) if is_internal_side else None

    # Source bank-account number (bank side) — tag each bank row per account.
    _stored = getattr(session, "stored_filename", None)
    _raw_path = _os.path.join(UPLOAD_DIR, _stored) if _stored else None
    _bank_acct = _extract_bank_account(
        getattr(session, "original_filename", "") or "", _raw_path
    ) if is_bank_side else None

    # ── Funds-position capture (mirror of routes/upload.py — contract #10) ──
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

        # ── Partner resolution ────────────────────────────────────────────────
        if is_mixed:
            src_val    = str(row_dict.get(source_column, '')).strip() if source_column else ''
            row_partner = _src_map.get(src_val) or _src_map.get(src_val.upper())
            if not row_partner:
                skipped += 1
                continue
        else:
            row_partner = session.partner
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

        # ── TYPENAME filter: applies to any partner whose preset declares it ────────
        # Covers aeps, pg, digikhata, indonepal (all use the combined Simplibank dump)
        if is_internal_side:
            from routes.upload import BANK_FORMAT_PRESETS, _detect_bank_format
            preset = _detect_bank_format(session.partner, session.side, df_columns)
            typename_filter = preset.get("typename_filter") if preset else None
            if typename_filter:
                typename_col = next(
                    (c for c in df_columns if c.strip().lower() == 'typename'), None
                )
                if typename_col:
                    row_typename = str(row_dict.get(typename_col, '')).strip()
                    if row_typename != typename_filter:
                        skipped += 1
                        continue

        # ── CR rows: NEVER drop any entry (data integrity policy) ───────────────
        # Previously CR rows were skipped for DMT internal on the assumption that
        # they were internal bookkeeping only. This caused reversal credit entries
        # (Status=Refunded, TYPENAME=Refund / Fund Transfer) to be silently lost.
        # Policy: ingest ALL rows. Refunded/reversed CR rows already get
        # recon_status="failed" via the FAILED_STATUSES check below, making them
        # visible in Open Items for manual review.
        # (No CR filter applied — all rows pass through)

        # ── Row classification ────────────────────────────────────────────────
        row_type          = "txn"
        auto_recon_status = "unmatched"

        if is_bank_side:
            if session.partner == "digikhata":
                from routes.upload import _detect_bank_format as _dbf
                _p = _dbf(session.partner, session.side, df_columns) or {}
                _kc = _p.get("kyc_product_col", "Product")
                _kv = _p.get("kyc_product_val", "eKYC Charge")
                _pcol = next((c for c in df_columns if c.strip() == _kc), None)
                if _pcol and str(row_dict.get(_pcol, '')).strip() == _kv:
                    row_type, auto_recon_status = "kyc_charge", "fee_matched"

            elif session.partner == "indonepal":
                # Indonepal: rows 1-2 (metadata title + filter string) are stripped
                # automatically by load_file_to_dataframe's header detection.
                # The Total summary row (last row, Sno="Total") must always be skipped.
                # Two-condition guard — both must pass for a row to be a real transaction:
                #   A: Sno is a positive integer
                #   B: PartnerPinNo is non-blank
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
                    continue  # Total / empty / metadata row — not a real transaction

            elif session.partner not in ("aeps", "pg"):
                row_type, auto_recon_status = _classify_bank_row(row_dict, df_columns)

        # ── Field mapping ─────────────────────────────────────────────────────
        eko_tid      = _clean(row_dict.get(mapping_dict.get("eko_tid",          ""), ""))
        tracking_num = _clean(row_dict.get(mapping_dict.get("tracking_number",  ""), ""))
        utr_num      = _clean(row_dict.get(mapping_dict.get("utr_number",       ""), ""))
        status       = _clean(row_dict.get(mapping_dict.get("status",           ""), ""))
        txn_date     = _clean(row_dict.get(mapping_dict.get("transaction_date", ""), ""))

        # ── Failed status detection (internal only) ───────────────────────────
        if is_internal_side and row_type == "txn" and status and status.lower() in FAILED_STATUSES:
            auto_recon_status = "failed"

        # ── Auto-date ─────────────────────────────────────────────────────────
        if is_auto_date:
            row_recon_date = _parse_recon_date(txn_date) if txn_date else session.recon_date
        else:
            row_recon_date = session.recon_date

        # ── Amount + DR/CR ────────────────────────────────────────────────────
        mapped_amt_col     = mapping_dict.get("amount", "")
        amount, dr_cr      = _auto_detect_amount(row_dict, df_columns, mapped_amt_col, is_bank_side)

        # AePS cashout bank statement: a "Withdrawal" transaction type = money out → DR.
        if session.partner == "aeps" and is_bank_side and not dr_cr:
            if any("withdrawal" in str(v).lower() for v in row_dict.values()):
                dr_cr = "DR"

        # ── Description parsing ───────────────────────────────────────────────
        desc_col = mapping_dict.get("description", "")
        desc_val = str(row_dict.get(desc_col, "")).strip() if desc_col else ""
        if not desc_val:
            for col_val in row_dict.values():
                v = str(col_val)
                if re.search(r'(?:IMPS|NEFT|RTGS)/', v, re.IGNORECASE):
                    desc_val = v
                    break

        if desc_val:
            parsed = _parse_description(desc_val)
            if not eko_tid      and parsed["eko_tid"]:         eko_tid      = parsed["eko_tid"]
            if not tracking_num and parsed["tracking_number"]: tracking_num = parsed["tracking_number"]
            if not utr_num      and parsed["utr_number"]:      utr_num      = parsed["utr_number"]

        # ── PG bank: extract net_amount ───────────────────────────────────────
        net_amount = None
        if session.partner == "pg" and is_bank_side:
            from routes.upload import BANK_FORMAT_PRESETS, _detect_bank_format
            preset = _detect_bank_format(session.partner, session.side, df_columns)
            net_amount_col = preset.get("net_amount_col") if preset else None
            if net_amount_col:
                net_amount = _safe_float(str(row_dict.get(net_amount_col, '')).replace(',', ''))

        # CSP (retailer) identity — internal dump only (read-only, additive).
        _csp_code, _csp_name = _extract_csp(row_dict) if is_internal_side else ("", "")

        # ── Funds-position: running balance + notification rows (mirror of
        #    routes/upload.py — contract #10; reporting only) ──
        _row_balance = None
        if is_bank_side:
            if _balance_col:
                _row_balance = _safe_float(str(row_dict.get(_balance_col, '')).replace(',', ''))
            _desc_u = (desc_val or "").strip().upper()
            if (row_type == "txn" and not dr_cr and (amount is None or amount == 0)
                    and not (eko_tid or tracking_num or utr_num)):
                row_type = "notification"
                auto_recon_status = "fund_transfer"
            _dr = _cr = 0.0
            if amount:
                if dr_cr == "DR":
                    _dr = amount
                elif dr_cr == "CR":
                    _cr = amount
                elif row_type in ("settlement_credit", "fund_transfer", "reversal"):
                    _cr = amount
                elif row_type != "notification":
                    _dr = amount
            _funds_lines.setdefault((row_partner, _bank_acct or ""), []).append({
                "date": row_recon_date, "balance": _row_balance, "dr": _dr, "cr": _cr,
                "stated_opening": _row_balance if _desc_u == "OPENING BALANCE" else None,
                "stated_closing": _row_balance if _desc_u == "CLOSING BALANCE" else None,
            })

        txns.append(Transaction(
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
            # Original bank narration for display (bank side only; "" not NULL
            # for no-narration bank rows so startup backfill only hits old rows).
            bank_description=(desc_val or "") if is_bank_side else None,
            # CSP code + name for display/search — internal side only; bank rows
            # keep NULL. "" (not NULL) when the dump carries no CSP column, so the
            # startup backfill only touches pre-existing rows. Read-only.
            csp_code=(_csp_code if is_internal_side else None),
            csp_name=(_csp_name if is_internal_side else None),
            # Running statement balance (bank side; display/funds-position only)
            balance=(_row_balance if is_bank_side else None),
        ))

    db.bulk_save_objects(txns)
    session.status    = UploadStatus.done
    session.row_count = len([t for t in txns if t.row_type == "txn"])
    db.commit()

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
                record_snapshots(db, _prod, _fp, _facct, _flines, uploaded_by="auto-upload")
        except Exception:
            pass   # snapshots must never block the watch-folder ingest

    # ── Reversal matching ─────────────────────────────────────────────────────
    reversal_results = {}
    if is_bank_side:
        from core.matching_engine import run_reversal_match
        bank_pairs = set((t.partner, t.recon_date) for t in txns
                         if t.row_type in ("reversal", "fee_reversal") and t.recon_date)
        for (p, d) in bank_pairs:
            if p and d:
                try:
                    rev_r = run_reversal_match(p, d, db, user_id)
                    if rev_r["reversal_matched"] > 0:
                        reversal_results[f"{p}/{d}"] = rev_r
                except Exception:
                    pass

    # ── Auto-recon: runs after BOTH bank AND internal uploads ─────────────────
    auto_recon_results = {}
    neft_d1_results    = {}
    if do_auto_recon:
        from core.matching_engine import run_reconciliation, run_neft_d1_match
        pairs = set((t.partner, t.recon_date) for t in txns if t.row_type == "txn" and t.recon_date)
        for (p, d) in pairs:
            if p and d:
                try:
                    r = run_reconciliation(p, d, db, user_id)
                    auto_recon_results[f"{p}/{d}"] = {
                        "matched":             r["matched"],
                        "unmatched_bank":      r["unmatched_bank"],
                        "unmatched_internal":  r["unmatched_internal"],
                    }
                except Exception:
                    pass
                if is_internal_side:   # NEFT D+1 only makes sense after internal upload
                    try:
                        neft_r = run_neft_d1_match(p, d, db, user_id)
                        if neft_r["neft_d1_matched"] > 0:
                            neft_d1_results[f"{p}/{d}"] = neft_r
                    except Exception:
                        pass
                    # Internal self-match: net off DR/CR contra pairs (and
                    # Success+Refund) within the dump. Runs after auto-recon/NEFT
                    # so bank-matched rows are left untouched. All products.
                    try:
                        from core.matching_engine import run_internal_match
                        run_internal_match(p, d, db, user_id)
                    except Exception:
                        pass

    # ── Same-side duplicate flagging (re-ingested rows) ───────────────────────
    # Mirrors routes/upload.py (contract #10): flag txn rows that duplicate an
    # already-present txn on the same (partner, side) as 'duplicate' (overlapping
    # or repeated uploads). Non-destructive; keeps the matched/first copy.
    duplicate_results = {}
    try:
        from core.matching_engine import flag_same_side_duplicates
        for p in set(t.partner for t in txns if t.row_type == "txn" and t.partner):
            dr = flag_same_side_duplicates(p, session.side, db)
            if dr["duplicates_flagged"] > 0:
                duplicate_results[f"{p}/{session.side}"] = dr
    except Exception:
        pass

    # ── Upload history ────────────────────────────────────────────────────────
    display_date = "auto (multi-date)" if is_auto_date else session.recon_date
    txn_count     = len([t for t in txns if t.row_type == "txn"])
    fee_count     = len([t for t in txns if t.row_type == "fee_charge"])
    cred_count    = len([t for t in txns if t.row_type == "settlement_credit"])
    rev_count     = len([t for t in txns if t.row_type in ("reversal", "fee_reversal")])
    fund_count    = len([t for t in txns if t.row_type in ("fund_transfer", "settlement_credit")])
    partner_counts = {}
    for t in txns:
        if t.row_type == "txn":
            partner_counts[t.partner] = partner_counts.get(t.partner, 0) + 1

    try:
        db.add(UploadHistory(
            user_id=user_id, username=username,
            filename=session.original_filename,
            partner=session.partner, side=session.side,
            recon_date=display_date,
            rows_inserted=txn_count,
            format_detected=session.column_mapping,
        ))
        db.commit()
    except Exception:
        db.rollback()

    # ── Audit log ─────────────────────────────────────────────────────────────
    try:
        db.add(AuditLog(
            user_id=user_id, username=username,
            action="upload", entity_type="upload_session", entity_id=session.id,
            detail=json.dumps({
                "filename":      session.original_filename,
                "partner":       session.partner,
                "side":          session.side,
                "recon_date":    display_date,
                "rows_inserted": txn_count,
                "partner_counts": partner_counts,
                "source":        "auto_upload" if username in ("auto-upload", "openclaw-bot") else "manual",
            }),
        ))
        db.commit()
    except Exception:
        db.rollback()

    return {
        "message":                 "Ingested successfully",
        "row_count":               txn_count,
        "fee_charge_count":        fee_count,
        "settlement_credit_count": cred_count,
        "fund_transfer_count":     fund_count,
        "reversal_count":          rev_count,
        "partner_counts":          partner_counts,
        "skipped":                 skipped,
        "session_id":              session.id,
        "auto_recon":              auto_recon_results if is_internal_side else {},
        "neft_d1_auto":            neft_d1_results    if is_internal_side else {},
        "reversal_auto":           reversal_results   if is_bank_side     else {},
        "duplicate_flagged":       duplicate_results,
    }
