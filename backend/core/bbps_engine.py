"""
core/bbps_engine.py

BBPS (Bill Payment / Recharge) reconciliation engine.

Reconciles EKO's internal Simplibank dump against the operator/aggregator
transaction reports from two providers:

  • Moneyart  → "TransactionReport*.xlsx"  (match key: ClientRef)
  • Levin     → "UtilityReport*.xlsx"      (match key: client_ref_id)

Matching: internal eko_trxn_id  ==  bank ClientRef / client_ref_id, with the
amount agreeing within ₹1. Status reconciliation captures the BBPS failure /
refund lifecycle:

  bank Success + internal not-refunded   → matched
  bank Failed  + internal Refunded        → failed_refunded      (correctly handled)
  bank Failed  + internal not-refunded    → failed_pending_refund (EXCEPTION — money stuck)
  bank Success + internal Refunded         → refunded_but_success (EXCEPTION — double pay risk)
  reference matches, amount differs        → amount_mismatch
  internal with no bank record             → unmatched_internal
  bank record with no internal             → unmatched_bank
"""
import io
import re
import datetime
import logging

import pandas as pd

logger = logging.getLogger("eko_recon.bbps")

AMOUNT_TOL = 1.0
RECON_OK = ("matched", "failed_refunded")  # statuses considered reconciled


# ─── helpers ──────────────────────────────────────────────────────────────────
def _sf(v, default=0.0) -> float:
    try:
        s = str(v).replace(",", "").replace("₹", "").strip()
        if s in ("", "-", "nan", "none", "None", "NaN", "\\N", "null", "NULL"):
            return default
        return float(s)
    except Exception:
        return default


def _clean(v) -> str:
    s = str(v).strip() if v is not None else ""
    if s.startswith("'"):                 # Excel text-format marker — artifact, never data
        s = s.lstrip("'").strip()         # (protects _classify status reads + the upsert identity)
    if s.lower() in ("nan", "none", "null", "\\n", "-"):
        return ""
    return s


def _norm_ref(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _norm_date(v) -> str:
    s = _clean(v)
    if not s:
        return ""
    s = s.split()[0] if (" " in s) else s
    s = s.strip()
    m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})$", s)
    if m:
        d, mo, y = m.groups()
        if len(y) == 2:
            y = "20" + y
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d/%b/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    try:
        import dateutil.parser
        return dateutil.parser.parse(str(v), dayfirst=True).date().isoformat()
    except Exception:
        return ""


def _provider_from_source(source: str) -> str:
    s = (source or "").lower()
    if "moneyart" in s:
        return "moneyart"
    if "levin" in s:
        return "levin"
    return (source or "").strip().lower() or "unknown"


# ─── Internal (Simplibank) parser ─────────────────────────────────────────────
def parse_internal(raw: bytes, filename: str) -> list:
    """
    Parse the SMB internal dump into one normalised record per eko_trxn_id.
    Keeps BBPS rows (TYPENAME 'Payments & Recharge'); a transaction that was
    refunded appears as DR + CR rows (Status 'Refunded') — we collapse it to a
    single record flagged is_refunded.
    """
    try:
        df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
    except Exception:
        df = pd.read_excel(io.BytesIO(raw), dtype=str).fillna("")
    df.columns = [c.strip() for c in df.columns]

    def g(row, *names):
        for n in names:
            if n in row and _clean(row[n]):
                return _clean(row[n])
        return ""

    by_id = {}
    for _, row in df.iterrows():
        typename = g(row, "TYPENAME")
        if typename and "payment" not in typename.lower() and "recharge" not in typename.lower():
            continue  # not a BBPS row
        eko = g(row, "eko_trxn_id")
        if not eko:
            continue
        dr_cr = g(row, "DEBIT_CREDIT").upper()
        status = g(row, "Status")
        amount = _sf(g(row, "Amount"))
        rec = by_id.get(eko)
        if rec is None:
            rec = by_id[eko] = {
                "eko_trxn_id": eko,
                "provider": _provider_from_source(g(row, "source")),
                "source": g(row, "source"),
                "amount": amount,
                "status": status,
                "is_refunded": False,
                "transaction_date": _norm_date(g(row, "Transaction_Date", "VALUE_DATE")),
                "csp_code": g(row, "CSPCode"),
                "merchant_name": g(row, "MerchantName"),
                "cell_number": g(row, "CellNumber"),
                "tracking_number": g(row, "TrackingNumber"),
                "typename": typename,
            }
        # DR row carries the canonical payment amount
        if dr_cr == "DR" and amount:
            rec["amount"] = amount
        if status and status.lower() in ("refunded", "reversed", "refund", "failed", "failure"):
            rec["is_refunded"] = True
            rec["status"] = "Refunded"
    return list(by_id.values())


# ─── Bank (operator) parsers — auto-detected ──────────────────────────────────
def detect_provider(columns: list) -> str:
    cols = {str(c).strip().lower() for c in columns}
    if "clientref" in cols or "operatorref" in cols:
        return "moneyart"
    if "client_ref_id" in cols or ("ref id" in cols and "recharge product" in cols):
        return "levin"
    return "unknown"


def parse_bank(raw: bytes, filename: str) -> tuple:
    """Auto-detect the operator and parse. Returns (provider, rows)."""
    df = pd.read_excel(io.BytesIO(raw), dtype=str).fillna("")
    df.columns = [c.strip() for c in df.columns]
    provider = detect_provider(df.columns)
    if provider == "unknown":
        # try CSV
        try:
            df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
            df.columns = [c.strip() for c in df.columns]
            provider = detect_provider(df.columns)
        except Exception:
            pass
    rows = []
    if provider == "moneyart":
        for _, r in df.iterrows():
            ref = _clean(r.get("ClientRef", ""))
            if not ref:
                continue
            rows.append({
                "provider": "moneyart", "client_ref": ref,
                "operator_ref": _clean(r.get("OperatorRef", "")),
                "order_id": _clean(r.get("OrderId", "")),
                "amount": _sf(r.get("Amount", 0)),
                "status": _clean(r.get("Status", "")),
                "transaction_date": _norm_date(r.get("TransactionDate", "")),
                "service_name": _clean(r.get("ServiceName", "")),
                "operator_name": _clean(r.get("OperatorName", "")),
                "reason": _clean(r.get("Reason", "")),
                "recharge_product": _clean(r.get("ServiceName", "")),
            })
    elif provider == "levin":
        for _, r in df.iterrows():
            ref = _clean(r.get("client_ref_id", ""))
            if not ref:
                continue
            rows.append({
                "provider": "levin", "client_ref": ref,
                "operator_ref": _clean(r.get("Ref Id", "")),
                "order_id": _clean(r.get("Id", "")),
                "amount": _sf(r.get("Amount", 0)),
                "status": _clean(r.get("Status", "")),
                "transaction_date": _norm_date(r.get("Date & Time", "")),
                "service_name": _clean(r.get("Recharge Product", "")),
                "operator_name": "",
                "reason": "",
                "recharge_product": _clean(r.get("Recharge Product", "")),
            })
    return provider, rows


def parse_bank_statement(raw: bytes, filename: str) -> tuple:
    provider, rows = parse_bank(raw, filename)
    if provider == "unknown":
        raise ValueError("Could not detect BBPS operator (expected Moneyart "
                         "TransactionReport or Levin UtilityReport columns).")
    return provider, rows


# ─── Matching engine ──────────────────────────────────────────────────────────
def _classify(bank, internal) -> str:
    """Return a recon status for a matched (ref-equal) bank↔internal pair."""
    amt_ok = abs(float(bank.get("amount") or 0) - float(internal.get("amount") or 0)) <= AMOUNT_TOL
    if not amt_ok:
        return "amount_mismatch"
    bstat = (bank.get("status") or "").lower()
    bank_failed = bstat.startswith("fail") or bstat in ("declined", "reject", "rejected")
    bank_success = bstat.startswith("succ") or bstat in ("paid", "completed")
    refunded = bool(internal.get("is_refunded"))
    if bank_success and not refunded:
        return "matched"
    if bank_failed and refunded:
        return "failed_refunded"
    if bank_failed and not refunded:
        return "failed_pending_refund"
    if bank_success and refunded:
        return "refunded_but_success"
    return "matched"


def reconcile(internal_rows: list, bank_rows: list) -> dict:
    """
    Match internal eko_trxn_id ↔ bank client_ref (+ amount). Annotates every
    row with recon_status / match_id / match_note and returns a summary.
    Existing recon state on the lists is reset first.
    """
    for i in internal_rows:
        i["recon_status"] = "unmatched_internal"; i["match_id"] = ""; i["match_note"] = ""
    for b in bank_rows:
        b["recon_status"] = "unmatched_bank"; b["match_id"] = ""; b["match_note"] = ""

    internal_by_ref = {}
    for i in internal_rows:
        internal_by_ref.setdefault(_norm_ref(i["eko_trxn_id"]), []).append(i)

    seq = [0]
    def new_mid():
        seq[0] += 1
        return f"BBPS-{datetime.date.today().strftime('%Y%m%d')}-{seq[0]:05d}"

    for b in bank_rows:
        cands = internal_by_ref.get(_norm_ref(b["client_ref"]))
        match = next((i for i in cands if i["recon_status"] == "unmatched_internal"), None) if cands else None
        if not match:
            continue
        status = _classify(b, match)
        mid = new_mid()
        note = f"{b['provider']} {b.get('status','')}"
        b["recon_status"] = status; b["match_id"] = mid; b["match_note"] = note
        match["recon_status"] = status; match["match_id"] = mid
        match["match_note"] = note + (f"; amt bank {b['amount']} vs int {match['amount']}" if status == "amount_mismatch" else "")

    STAT = ["matched", "failed_refunded", "failed_pending_refund", "refunded_but_success",
            "amount_mismatch", "unmatched_internal", "unmatched_bank"]
    summary = {s: 0 for s in STAT}
    for b in bank_rows:
        summary[b["recon_status"]] = summary.get(b["recon_status"], 0) + 1
    summary["unmatched_internal"] = sum(1 for i in internal_rows if i["recon_status"] == "unmatched_internal")
    summary["internal_rows"] = len(internal_rows)
    summary["bank_rows"] = len(bank_rows)
    reconciled = sum(1 for b in bank_rows if b["recon_status"] in RECON_OK)
    summary["reconciled"] = reconciled
    summary["exceptions"] = (summary["failed_pending_refund"] + summary["refunded_but_success"]
                             + summary["amount_mismatch"])
    summary["match_rate"] = round(reconciled / (len(bank_rows) or 1) * 100, 1)
    return {"internal_rows": internal_rows, "bank_rows": bank_rows, "summary": summary}
