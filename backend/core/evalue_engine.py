"""
core/evalue_engine.py

E-Value (wallet load) reconciliation engine.

Reconciles CSP wallet-load requests (internal Simplibank dump,
SMB_BANK_LOADIN) against credits in our bank accounts across 8 banks.

Two main flows:
  • ONLINE (IMPS / UPI / NEFT / RTGS / Intra)  → match on UTR + amount (same day)
  • CASH   (CDM / Non-CDM / Counter / Challan) → match on ATM/txn no + amount + date
                                                  + branch code + mobile

Plus detection of: twice-credit, wrong-amount credit, transaction fee, bank debit.

The module is intentionally self-contained: bank statement parsers + a
normaliser + the matching engine. Each parser converts a heterogeneous bank
statement into a list of normalised dicts with this schema:

    {
      "txn_date":   "YYYY-MM-DD",
      "value_date": "YYYY-MM-DD" | "",
      "description":str,
      "dr_cr":      "CR" | "DR",
      "amount":     float,           # always positive
      "balance":    float | None,
      "ref_no":     str,             # cheque/ref column if present
      "utr":        str,             # extracted online reference (UTR/IMPS/UPI)
      "atm_ref":    str,             # extracted cash/CDM machine/txn number
      "branch":     str,             # branch name or code
      "mobile":     str,             # 10-digit mobile if present in narration
      "channel":    str,             # IMPS|UPI|NEFT|RTGS|INTRA|CASH|CDM|FEE|DEBIT|OTHER
    }
"""

import io
import re
import datetime
import logging

import pandas as pd

logger = logging.getLogger("eko_recon.evalue")


# ─── Generic helpers ──────────────────────────────────────────────────────────
def _sf(v, default=0.0) -> float:
    """Safe float — strips commas, currency symbols, Cr./Dr. suffixes, spaces."""
    try:
        s = str(v)
        s = s.replace(",", "").replace("₹", "").replace("INR", "")
        s = re.sub(r"(?i)\b[cd]r\.?\b", "", s).strip()
        if s in ("", "-", "'-", "nan", "none", "None", "NaN"):
            return default
        return float(s)
    except Exception:
        return default


def _clean(v) -> str:
    s = str(v).strip() if v is not None else ""
    if s.startswith("'"):                 # Excel text-format marker — artifact, never data
        s = s.lstrip("'").strip()         # (protects the Success/DR eligibility filter + upsert identity;
    if s.lower() in ("nan", "none", "null", "\\n", "'-", "-"):   # match keys were already _norm_ref-immune)
        return ""
    return s


def _norm_date(v) -> str:
    """Normalise many date formats to YYYY-MM-DD. Returns '' if unparseable."""
    s = _clean(v)
    if not s:
        return ""
    s = s.split()[0] if " " in s and ":" in s else s   # drop time part if 'date time'
    s = s.strip()
    # DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})$", s)
    if m:
        d, mo, y = m.groups()
        if len(y) == 2:
            y = "20" + y
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    # YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    # DD-Mon-YYYY  (19-Nov-2025) or DD Mon YYYY
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d/%b/%Y", "%d-%b-%y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    try:
        import dateutil.parser
        return dateutil.parser.parse(s, dayfirst=True).date().isoformat()
    except Exception:
        return ""


# ─── Field extractors from narration ──────────────────────────────────────────
_CHANNEL_PATTERNS = [
    ("IMPS",  re.compile(r"\bIMPS", re.I)),
    ("UPI",   re.compile(r"\bUPI\b|@", re.I)),
    ("NEFT",  re.compile(r"\bNEFT", re.I)),
    ("RTGS",  re.compile(r"\bRTGS", re.I)),
    ("CDM",   re.compile(r"\bCDM\b|CSH\s*DEP|CASH\s*DEP|CASH\s*RECEIPT|ATM\s*DEP", re.I)),
    ("INTRA", re.compile(r"\bVIRP\b|\bTRF\b|INTRA|FT\b|FUND\s*TRANSFER", re.I)),
]

_FEE_RE = re.compile(r"\b(MAB|SMS|CHARGE|CHRG|GST|FEE|COMMISSION|COMMN|MIN\.?\s*BAL|"
                     r"SERVICE\s*CHARGE|NEFT\s*CHG|RTGS\s*CHG|IMPS\s*CHG|"
                     r"INCIDENTAL|PENAL|AMC|TDS)\b", re.I)


def _detect_channel(desc: str, dr_cr: str) -> str:
    d = desc or ""
    if dr_cr == "DR":
        if _FEE_RE.search(d):
            return "FEE"
        return "DEBIT"
    for name, pat in _CHANNEL_PATTERNS:
        if pat.search(d):
            return name
    return "OTHER"


def _extract_utr(desc: str) -> str:
    """
    Extract the online-transfer reference (UTR / IMPS ref / RTGS UTR) from
    a bank narration. Strategy: look right after the channel keyword.
    """
    d = desc or ""
    # RTGS/<UTR>/...  NEFT/<UTR>/...  also XXXXR5..., bank UTRs are 16-22 alnum
    m = re.search(r"(?:RTGS|NEFT)[/ \-:]+([A-Z0-9]{12,25})", d, re.I)
    if m:
        return m.group(1).upper()
    # IMPS 532311899877  or  IMPS/532.../  or IMPSAB/532.../
    m = re.search(r"IMPS[A-Z]*[/ \-:]+(\d{9,18})", d, re.I)
    if m:
        return m.group(1)
    # VIRP/C/IDB1230099106742/...
    m = re.search(r"VIRP[/ ]+[A-Z][/ ]+([A-Z0-9]{8,25})", d, re.I)
    if m:
        return m.group(1).upper()
    # UPI/<ref>/...  or UPI-<12digits>
    m = re.search(r"UPI[/ \-:]+([A-Z0-9]{6,25})", d, re.I)
    if m:
        return m.group(1).upper()
    # Generic: a long 12-22 digit number anywhere (settlement UTRs)
    m = re.search(r"\b(\d{12,22})\b", d)
    if m:
        return m.group(1)
    # Generic alnum bank-UTR token (e.g. HDFCR5..., YS5323...)
    m = re.search(r"\b([A-Z]{2,5}[A-Z0-9]{10,20})\b", d)
    if m:
        return m.group(1).upper()
    return ""


def _extract_atm_ref(desc: str) -> str:
    """Extract a CDM/ATM machine or transaction number from a cash narration."""
    d = desc or ""
    # CSH DEP (CDM)-9993998400  8574  → grab the longest digit run
    m = re.search(r"(?:CDM|ATM\s*DEP|CSH\s*DEP|CASH\s*DEP)[^\d]*(\d{4,})", d, re.I)
    if m:
        return m.group(1)
    nums = re.findall(r"\d{4,}", d)
    return max(nums, key=len) if nums else ""


def _extract_mobile(desc: str) -> str:
    """Extract a 10-digit Indian mobile number (6-9 lead) from narration."""
    for m in re.findall(r"\b([6-9]\d{9})\b", desc or ""):
        return m
    return ""


def _norm_ref(s: str) -> str:
    """Canonicalise a reference/UTR for comparison."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _ref_digits(*parts, minlen: int = 10) -> set:
    """All distinct digit-runs of >= minlen across the given strings.

    A bank RRN/UTR core (e.g. 52026060767796815, 615812656582) is a long unique
    digit run. Comparing these runs gives a PRECISE reference match that doesn't
    depend on prefixes (HDFCR…), the UTRNUMBER column, or fuzzy heuristics.
    """
    out = set()
    for p in parts:
        out.update(re.findall(r"\d{%d,}" % minlen, str(p or "")))
    return out


def _ref_match(bank_parts, load_parts) -> bool:
    """True if the bank reference shares a long digit-run with the load reference.
    `bank_parts` / `load_parts` are iterables of candidate strings (utr,
    description / tid_chequeno, bank_ref / Is_Weekend_Loan)."""
    bd = _ref_digits(*bank_parts)
    if not bd:
        return False
    return bool(bd & _ref_digits(*load_parts))


def _utr_match(a: str, b: str) -> bool:
    """
    Fuzzy UTR equality. Banks and the internal dump often store the same UTR
    with different prefixes/length:
      bank 'HDFCR52025111985035694'  ==  internal 'R52025111985035694'  (IFSC prefix dropped)
      bank 'UJVNR92025111900878872'  ~=  internal 'UJVNR25323878872'    (shared trailing seq)
    Match if: equal, one is a suffix/substring of the other, or their trailing
    8+ digit runs are equal.
    """
    na, nb = _norm_ref(a), _norm_ref(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 8 and len(nb) >= 8 and (na.endswith(nb) or nb.endswith(na) or na in nb or nb in na):
        return True
    # trailing digit run (transaction sequence) of 8+ digits
    da = re.findall(r"\d{8,}", na)
    db = re.findall(r"\d{8,}", nb)
    if da and db:
        ta, tb = da[-1], db[-1]
        if ta == tb or ta.endswith(tb[-8:]) or tb.endswith(ta[-8:]):
            return True
    return False


# ─── Per-bank statement parsers ───────────────────────────────────────────────
# Each takes raw bytes + filename, returns list[dict] in the normalised schema.

def _finalise_rows(records: list) -> list:
    """Apply channel/utr/atm/mobile extraction to raw {date,desc,dr_cr,amount,...} rows."""
    out = []
    for r in records:
        desc = r.get("description", "")
        dr_cr = r.get("dr_cr", "")
        amount = float(r.get("amount") or 0)
        if amount == 0 and not desc:
            continue
        channel = _detect_channel(desc, dr_cr)
        out.append({
            "txn_date":   r.get("txn_date", ""),
            "value_date": r.get("value_date", ""),
            "description": desc,
            "dr_cr":      dr_cr,
            "amount":     round(abs(amount), 2),
            "balance":    r.get("balance"),
            "ref_no":     r.get("ref_no", ""),
            "utr":        r.get("utr") or (_extract_utr(desc) if dr_cr == "CR" else ""),
            "atm_ref":    r.get("atm_ref") or (_extract_atm_ref(desc) if channel == "CDM" else ""),
            "branch":     r.get("branch", ""),
            "mobile":     r.get("mobile") or _extract_mobile(desc),
            "channel":    channel,
        })
    return out


def _find_header_row(df: pd.DataFrame, must_have: list, max_scan: int = 60):
    """Return the row index whose cell values contain all `must_have` substrings (case-insensitive)."""
    must = [m.lower() for m in must_have]
    for i in range(min(max_scan, len(df))):
        cells = " | ".join(_clean(x).lower() for x in df.iloc[i].tolist())
        if all(m in cells for m in must):
            return i
    return None


def _split_dr_cr(amount_val, indicator: str):
    """Given a single signed-amount column gated by a CR/DR indicator, return (dr_cr, amount)."""
    ind = _clean(indicator).upper().replace(".", "")
    amt = _sf(amount_val)
    dr_cr = "CR" if ind.startswith("C") else ("DR" if ind.startswith("D") else "")
    return dr_cr, abs(amt)


def parse_axis(raw: bytes, filename: str) -> list:
    """
    Axis CSV. The 'Transaction Particulars' column contains commas (and stray
    quotes), which broke the naive read_csv (rows were dropped / mis-aligned and
    the narration / UTR was lost). We instead split each line positionally:
    the layout is fixed at 8 fields —
      Tran Date, Value Date, CHQNO, Transaction Particulars, Debit, Credit, Balance, Branch
    so we take the first 3 fields from the left, the last 4 from the right, and
    rejoin everything in between as the Particulars (commas preserved).
    """
    text = raw.decode("latin-1", errors="replace")
    lines = text.splitlines()
    # locate the header row dynamically (tolerant of header-block size drift)
    hi = None
    for i, ln in enumerate(lines[:40]):
        low = ln.lower()
        if "tran date" in low and "particular" in low:
            hi = i
            break
    if hi is None:
        return []

    def _unq(s):
        s = s.strip()
        return s[1:-1].strip() if len(s) >= 2 and s[0] == s[-1] == '"' else s

    recs = []
    for ln in lines[hi + 1:]:
        if not ln.strip():
            continue
        parts = ln.split(",")
        if len(parts) < 8:
            continue
        d = _norm_date(_unq(parts[0]))
        if not d:
            continue
        particulars = _unq(",".join(parts[3:-4]))
        debit   = _sf(_unq(parts[-4]))
        credit  = _sf(_unq(parts[-3]))
        balance = _sf(_unq(parts[-2]))
        branch  = _unq(parts[-1])
        dr_cr = "CR" if credit > 0 else ("DR" if debit > 0 else "")
        recs.append({
            "txn_date": d, "value_date": _norm_date(_unq(parts[1])),
            "description": particulars,
            "dr_cr": dr_cr, "amount": credit if credit > 0 else debit,
            "balance": balance, "ref_no": _unq(parts[2]), "branch": branch,
        })
    return _finalise_rows(recs)


def parse_pnb(raw: bytes, filename: str) -> list:
    text = raw.decode("latin-1", errors="replace")
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False,
                     header=None, engine="python", on_bad_lines="skip")
    hi = _find_header_row(df, ["txn date", "cr amount"])
    if hi is None:
        hi = _find_header_row(df, ["txn date", "amount"])
    df = pd.read_csv(io.StringIO(text), skiprows=hi, dtype=str, keep_default_na=False,
                     engine="python", on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]
    recs = []
    for _, row in df.iterrows():
        d = _norm_date(row.get("Txn Date", ""))
        if not d:
            continue
        cr = _sf(row.get("Cr Amount", 0))
        dr = _sf(row.get("Dr Amount", 0))
        recs.append({
            "txn_date": d, "value_date": "",
            "description": _clean(row.get("Description", "")),
            "dr_cr": "CR" if cr > 0 else ("DR" if dr > 0 else ""),
            "amount": cr if cr > 0 else dr,
            "balance": _sf(row.get("Balance", 0)),
            "ref_no": _clean(row.get("Cheque No.", "")) or _clean(row.get("Txn No.", "")),
            "branch": _clean(row.get("Branch Name", "")),
        })
    return _finalise_rows(recs)


def parse_boi(raw: bytes, filename: str) -> list:
    tables = pd.read_html(io.BytesIO(raw))
    # Pick the table that has a Cr/Dr + Amount header
    df = None
    for t in tables:
        cols = " ".join(str(c).lower() for c in t.columns)
        if "amount" in cols and ("cr/dr" in cols or "description" in cols):
            df = t
            break
    if df is None:
        df = max(tables, key=lambda t: t.shape[0])
    df.columns = [str(c).strip() for c in df.columns]
    recs = []
    for _, row in df.iterrows():
        d = _norm_date(row.get("Txn Date", ""))
        if not d:
            continue
        dr_cr, amt = _split_dr_cr(row.get("Amount(INR)", 0), row.get("Cr/Dr", ""))
        recs.append({
            "txn_date": d, "value_date": "",
            "description": _clean(row.get("Description", "")),
            "dr_cr": dr_cr, "amount": amt,
            "balance": _sf(row.get("Balance(INR)", 0)),
            "ref_no": _clean(row.get("ChequeNo.", "")),
            "branch": "",
        })
    return _finalise_rows(recs)


def parse_sbi(raw: bytes, filename: str) -> list:
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    hi = next((i for i, l in enumerate(lines) if l.lower().startswith("txn date")), None)
    if hi is None:
        return []
    recs = []
    for l in lines[hi + 1:]:
        parts = l.split("\t")
        if len(parts) < 8:
            continue
        d = _norm_date(parts[0])
        if not d:
            continue
        dr = _sf(parts[5]); cr = _sf(parts[6])
        recs.append({
            "txn_date": d, "value_date": _norm_date(parts[1]),
            "description": _clean(parts[2]),
            "dr_cr": "CR" if cr > 0 else ("DR" if dr > 0 else ""),
            "amount": cr if cr > 0 else dr,
            "balance": _sf(parts[7]),
            "ref_no": _clean(parts[3].split("/")[-1]),
            "branch": _clean(parts[4]),
        })
    return _finalise_rows(recs)


def _parse_ole_generic(raw: bytes, must_have: list, colmap: dict, signed=False) -> list:
    """
    Generic OLE/.xls parser. `colmap` maps normalised field -> header substring.
    Used for IDBI, RBL, UBI by passing different column maps.
    """
    df0 = pd.read_excel(io.BytesIO(raw), header=None, engine="xlrd")
    hi = _find_header_row(df0, must_have)
    if hi is None:
        return []
    header = [_clean(x) for x in df0.iloc[hi].tolist()]
    data = df0.iloc[hi + 1:].reset_index(drop=True)
    data.columns = range(data.shape[1])

    def col_idx(sub):
        sub = sub.lower()
        for j, h in enumerate(header):
            if sub in h.lower():
                return j
        return None

    idx = {k: col_idx(v) for k, v in colmap.items()}
    recs = []
    for _, row in data.iterrows():
        dval = row[idx["txn_date"]] if idx.get("txn_date") is not None else ""
        d = _norm_date(dval)
        if not d:
            continue
        desc = _clean(row[idx["description"]]) if idx.get("description") is not None else ""
        if signed:
            dr_cr, amt = _split_dr_cr(
                row[idx["amount"]] if idx.get("amount") is not None else 0,
                row[idx["dr_cr"]] if idx.get("dr_cr") is not None else "",
            )
        else:
            cr = _sf(row[idx["credit"]]) if idx.get("credit") is not None else 0
            dr = _sf(row[idx["debit"]]) if idx.get("debit") is not None else 0
            dr_cr = "CR" if cr > 0 else ("DR" if dr > 0 else "")
            amt = cr if cr > 0 else dr
        rec = {
            "txn_date": d,
            "value_date": _norm_date(row[idx["value_date"]]) if idx.get("value_date") is not None else "",
            "description": desc, "dr_cr": dr_cr, "amount": amt,
            "balance": _sf(row[idx["balance"]]) if idx.get("balance") is not None else None,
            "ref_no": _clean(row[idx["ref_no"]]) if idx.get("ref_no") is not None else "",
            "branch": _clean(row[idx["branch"]]) if idx.get("branch") is not None else "",
            "utr": _clean(row[idx["utr"]]) if idx.get("utr") is not None else "",
        }
        recs.append(rec)
    return _finalise_rows(recs)


def parse_idbi(raw: bytes, filename: str) -> list:
    return _parse_ole_generic(
        raw, must_have=["txn date", "amount"],
        colmap={"txn_date": "txn date", "value_date": "value date",
                "description": "description", "dr_cr": "cr/dr",
                "amount": "amount", "balance": "balance", "ref_no": "cheque"},
        signed=True)


def parse_rbl(raw: bytes, filename: str) -> list:
    return _parse_ole_generic(
        raw, must_have=["transaction date", "deposit"],
        colmap={"txn_date": "transaction date", "value_date": "value date",
                "description": "transaction details", "credit": "deposit",
                "debit": "withdraw", "balance": "balance", "ref_no": "cheque"},
        signed=False)


def parse_ubi(raw: bytes, filename: str) -> list:
    return _parse_ole_generic(
        raw, must_have=["date", "deposits"],
        colmap={"txn_date": "date", "description": "remarks", "credit": "deposits",
                "debit": "withdrawal", "balance": "balance", "utr": "utr",
                "ref_no": "tran id"},
        signed=False)


def parse_idfc(raw: bytes, filename: str) -> list:
    xl = pd.ExcelFile(io.BytesIO(raw), engine="openpyxl")
    sheet = next((s for s in xl.sheet_names if "statement" in s.lower()), xl.sheet_names[0])
    df0 = xl.parse(sheet, header=None)
    hi = _find_header_row(df0, ["transaction date", "credit"])
    if hi is None:
        return []
    header = [_clean(x) for x in df0.iloc[hi].tolist()]
    data = df0.iloc[hi + 1:].reset_index(drop=True)
    data.columns = range(data.shape[1])

    def ci(sub):
        for j, h in enumerate(header):
            if sub in h.lower():
                return j
        return None

    cD, cV, cP = ci("transaction date"), ci("value date"), ci("particular")
    cDeb, cCr, cBal, cChq = ci("debit"), ci("credit"), ci("balance"), ci("cheque")
    recs = []
    for _, row in data.iterrows():
        d = _norm_date(row[cD]) if cD is not None else ""
        if not d:
            continue
        cr = _sf(row[cCr]) if cCr is not None else 0
        dr = _sf(row[cDeb]) if cDeb is not None else 0
        recs.append({
            "txn_date": d, "value_date": _norm_date(row[cV]) if cV is not None else "",
            "description": _clean(row[cP]) if cP is not None else "",
            "dr_cr": "CR" if cr > 0 else ("DR" if dr > 0 else ""),
            "amount": cr if cr > 0 else dr,
            "balance": _sf(row[cBal]) if cBal is not None else None,
            "ref_no": _clean(row[cChq]) if cChq is not None else "",
            "branch": "",
        })
    return _finalise_rows(recs)


def parse_icici(raw: bytes, filename: str) -> list:
    """
    ICICI corporate 'Detailed Statement' (.xlsx, openpyxl). ~16-row header block
    (name/address/period/advanced-search), then:
      S.N. | Tran. Id | Value Date | Transaction Date | Transaction Posted Date |
      Cheque. No./Ref. No. | Transaction Remarks | Withdrawal Amt (INR) |
      Deposit Amt (INR) | Balance (INR)
    Rows are chronological; a footer legend follows the data (no dates → skipped).
    Dates like '01/Jul/2026'; amounts with Indian comma grouping.
    """
    df0 = pd.read_excel(io.BytesIO(raw), header=None, engine="openpyxl")
    hi = _find_header_row(df0, ["tran. id", "withdrawal"])
    if hi is None:
        hi = _find_header_row(df0, ["transaction date", "deposit amt"])
    if hi is None:
        return []
    header = [_clean(x) for x in df0.iloc[hi].tolist()]
    data = df0.iloc[hi + 1:].reset_index(drop=True)
    data.columns = range(data.shape[1])

    def ci(sub):
        for j, h in enumerate(header):
            if sub in h.lower():
                return j
        return None

    cD, cV = ci("transaction date"), ci("value date")   # 'transaction date' hits before 'transaction posted date'
    cP, cRef, cTid = ci("remarks"), ci("cheque"), ci("tran. id")
    cDeb, cCr, cBal = ci("withdrawal"), ci("deposit"), ci("balance")
    recs = []
    for _, row in data.iterrows():
        d = _norm_date(row[cD]) if cD is not None else ""
        if not d:
            continue
        cr = _sf(row[cCr]) if cCr is not None else 0
        dr = _sf(row[cDeb]) if cDeb is not None else 0
        recs.append({
            "txn_date": d, "value_date": _norm_date(row[cV]) if cV is not None else "",
            "description": _clean(row[cP]) if cP is not None else "",
            "dr_cr": "CR" if cr > 0 else ("DR" if dr > 0 else ""),
            "amount": cr if cr > 0 else dr,
            "balance": _sf(row[cBal]) if cBal is not None else None,
            "ref_no": (_clean(row[cRef]) if cRef is not None else "")
                      or (_clean(row[cTid]) if cTid is not None else ""),
            "branch": "",
        })
    return _finalise_rows(recs)


# Bank name (normalised) → parser
BANK_PARSERS = {
    "AXIS BANK":            parse_axis,
    "BANK OF INDIA":        parse_boi,
    "ICICI BANK":           parse_icici,
    "IDBI BANK":            parse_idbi,
    "IDFC BANK":            parse_idfc,
    "PUNJAB NATIONAL BANK": parse_pnb,
    "RBL BANK":             parse_rbl,
    "SBI":                  parse_sbi,
    "UNION BANK OF INDIA":  parse_ubi,
}


def parse_bank_statement(bank_name: str, raw: bytes, filename: str) -> list:
    """Dispatch to the right parser by bank name. Raises ValueError if unsupported."""
    key = (bank_name or "").strip().upper()
    parser = BANK_PARSERS.get(key)
    if not parser:
        raise ValueError(f"No parser for bank '{bank_name}'. Supported: {sorted(BANK_PARSERS)}")
    return parser(raw, filename)


# ─── Internal dump parser (SMB_BANK_LOADIN) ───────────────────────────────────
def parse_internal_dump(raw: bytes, filename: str) -> list:
    """
    Parse the Simplibank E-Value load-in dump into normalised wallet-load rows.
    `source` column = RECO-ACC-NO (the bank account the load is expected in).
    """
    try:
        df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
    except Exception:
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
        df = df.fillna("")
    df.columns = [c.strip() for c in df.columns]

    def g(row, *names):
        for n in names:
            if n in row and _clean(row[n]):
                return _clean(row[n])
        return ""

    out = []
    for _, row in df.iterrows():
        amount = _sf(g(row, "Amount"))
        reco = g(row, "source")
        # UTR candidate: UTRNUMBER, else TID_ChequeNo, else COMMENTS (for online)
        utr = g(row, "UTRNUMBER") or g(row, "TID_ChequeNo") or g(row, "BANK_TID")
        out.append({
            "reco_acc_no":    reco,
            "eko_trxn_id":    g(row, "eko_trxn_id"),
            "account_trxn_id":g(row, "ACCOUNT_TRXN_ID"),
            "csp_code":       g(row, "CSPCode"),
            "merchant_name":  g(row, "MerchantName"),
            "cell_number":    g(row, "CellNumber"),
            "amount":         round(amount, 2),
            "transaction_date": _norm_date(g(row, "Transaction_Date")),
            "value_date":     _norm_date(g(row, "VALUE_DATE")),
            "status":         g(row, "Status"),
            "typename":       g(row, "TYPENAME"),
            "dr_cr":          g(row, "DEBIT_CREDIT"),
            "mode":           g(row, "Mode"),
            "utr_number":     utr,
            "tid_chequeno":   g(row, "TID_ChequeNo"),
            # Is_Weekend_Loan column carries the full bank reference (e.g.
            # "HDFCR52026060767796815" or "IMPS615816953890STARCOMM…").
            "bank_ref":       g(row, "Is_Weekend_Loan"),
            "cdm_txn_number": g(row, "cdm_transaction_number"),
            "cdm_branch":     g(row, "CDM_Branch_Name"),
            "branch_name":    g(row, "Branch_Name"),
            "branch_code":    g(row, "BranchCode"),
            "remarks":        g(row, "Remarks"),
            "comments":       g(row, "COMMENTS"),
            "provider_type":  g(row, "ProviderType"),
        })
    return out


def classify_load_mode(load: dict) -> str:
    """Decide whether an internal wallet-load row is ONLINE or CASH."""
    txt = f"{load.get('remarks','')} {load.get('comments','')} {load.get('mode','')}".lower()
    if any(k in txt for k in ("cash", "cdm", "challan", "counter", "atm dep")):
        return "CASH"
    if load.get("cdm_txn_number") or load.get("cdm_branch"):
        return "CASH"
    # Online if a UTR-like reference is present
    if load.get("utr_number"):
        return "ONLINE"
    return "ONLINE"


# ─── Per-bank amount tolerance (configurable) ─────────────────────────────────
# Default policy is exact-amount; a few banks round cash deposits — set a small
# rupee tolerance here (or override via reconcile(amount_tol=...)).
BANK_TOLERANCE = {
    # "AXIS BANK": 1.0,
}
# Internal "source" aliases — some products tag cash with a non-account source.
# e.g. Axis cash loads carry source "Axis CDM" rather than the RECO-ACC-NO.
SOURCE_ALIASES = {
    "AXIS": ["AXISCDM"],   # any AXIS-xxxx account also considers "Axis CDM" loads
}


def _acct_aliases(reco_acc_no: str) -> set:
    keys = {_norm_ref(reco_acc_no)}
    for prefix, aliases in SOURCE_ALIASES.items():
        if _norm_ref(reco_acc_no).startswith(prefix):
            keys.update(_norm_ref(a) for a in aliases)
    return keys


# ─── Matching engine ──────────────────────────────────────────────────────────
def reconcile(bank_rows: list, loads: list, reco_acc_no: str, amount_tol: float = 0.0,
              bank_name: str = "") -> dict:
    """
    Reconcile bank statement credits against internal wallet loads for ONE account.

    Returns a dict with classified bank_rows and loads (each annotated with
    `recon_status`, `match_id`, `match_note`) plus a summary.

    amount_tol: rupee tolerance for amount comparison (0 = exact). Falls back to
    BANK_TOLERANCE[bank_name] when amount_tol is 0 and a bank entry exists.

    Rules (per Rajendra 2026-07-14 — amount AND date must BOTH match to link):
      ONLINE: bank credit reference/UTR matches a load AND amount~= AND SAME date
      CASH:   amount~= AND same date AND (atm/txn no OR branch OR mobile match)
      Extra bank credit matching an already-matched load  → twice_credit
      Unmatched bank credit → unmatched_bank ; unmatched load → unmatched_load
      Bank DR rows → classified FEE (transaction_fee) or bank_debit
      An identifier match whose amount OR date differs is left OPEN (unmatched) —
      the auto-matcher NEVER creates a 'wrong_amount' link. (wrong_amount now comes
      only from deliberate MANUAL matches in routes/evalue.py.)
    """
    if not amount_tol:
        amount_tol = BANK_TOLERANCE.get((bank_name or "").strip().upper(), 0.0)
    _alias = _acct_aliases(reco_acc_no)
    # Only loads for this account (incl. source aliases), successful, credit side
    acct_loads = [l for l in loads
                  if _norm_ref(l.get("reco_acc_no")) in _alias
                  and l.get("status", "").lower() in ("success", "")
                  and l.get("dr_cr", "CR").upper() != "DR"]
    for l in acct_loads:
        l["recon_status"] = "unmatched_load"
        l["match_id"] = ""
        l["match_note"] = ""
        l["_mode"] = classify_load_mode(l)

    credits = [b for b in bank_rows if b["dr_cr"] == "CR"]
    debits  = [b for b in bank_rows if b["dr_cr"] == "DR"]
    for b in bank_rows:
        b["recon_status"] = ""
        b["match_id"] = ""
        b["match_note"] = ""

    mid_seq = [0]
    def new_mid():
        mid_seq[0] += 1
        return f"EV-{_norm_ref(reco_acc_no)}-{mid_seq[0]:05d}"

    # Index loads by (date, amount) and by utr for fast lookup
    def amt_key(a):
        return round(float(a or 0), 2)

    def amt_match(a, b):
        return abs(float(a or 0) - float(b or 0)) <= amount_tol if amount_tol \
            else amt_key(a) == amt_key(b)

    # Internal reconciliation date = the load's VALUE date (Rajendra, finance ops,
    # 2026-07-15). The bank credits an E-Value load on its value date, not its
    # transaction/initiation date, so the value date is what must equal the bank's
    # single statement date. Fall back to the transaction date only when the value
    # date is blank. Both dates stay stored on the row — only this MATCH comparison
    # switched from transaction_date to value_date.
    def load_date(l):
        return l.get("value_date") or l.get("transaction_date") or ""

    # ── 0) REFERENCE match (PRIMARY) ──────────────────────────────────────────
    # Per finance ops: match the bank statement Particulars reference (e.g.
    # HDFCR52026060767796815) against the load's TID_ChequeNo / Is_Weekend_Loan,
    # NOT the Eko TID. Precise: a shared 10+ digit RRN. Independent of the
    # UTRNUMBER column (often NULL for these RTGS/IMPS loads).
    for b in credits:
        if b["recon_status"]:
            continue
        bank_parts = (b.get("utr"), b.get("description"), b.get("ref_no"))
        ref_cands = [l for l in acct_loads
                     if l["recon_status"] == "unmatched_load"
                     and _ref_match(bank_parts, (l.get("tid_chequeno"), l.get("bank_ref"), l.get("utr_number")))]
        if not ref_cands:
            continue
        # amount AND date must both match — an identifier match whose amount or date
        # differs is left OPEN (no wrong_amount link). Date = the load's VALUE date.
        amt_ok = [l for l in ref_cands
                  if amt_match(l["amount"], b["amount"]) and load_date(l) == b["txn_date"]]
        if amt_ok:
            l = amt_ok[0]; mid = new_mid()
            b["recon_status"] = "matched_online"; b["match_id"] = mid; b["match_note"] = "reference + amount + date"
            l["recon_status"] = "matched_online"; l["match_id"] = mid; l["match_note"] = "reference + amount + date"

    # ── 1) ONLINE match on UTR + amount + date ────────────────────────────────
    for b in credits:
        if b["recon_status"]:
            continue
        butr = b.get("utr")
        if not _norm_ref(butr):
            continue
        # loads whose UTR matches (fuzzy: prefix/suffix tolerant)
        utr_cands = [l for l in acct_loads
                     if l["recon_status"] == "unmatched_load"
                     and _utr_match(butr, l.get("utr_number"))]
        # STRICT: UTR + exact amount + SAME date only. A UTR match whose amount or
        # date differs is left OPEN — no cross-date match, no wrong_amount link.
        # Date = the load's VALUE date.
        exact = [l for l in utr_cands
                 if amt_match(l["amount"], b["amount"])
                 and load_date(l) == b["txn_date"]]
        if exact:
            l = exact[0]
            mid = new_mid()
            b["recon_status"] = "matched_online"; b["match_id"] = mid; b["match_note"] = "UTR+amount+date"
            l["recon_status"] = "matched_online"; l["match_id"] = mid; l["match_note"] = "UTR+amount+date"
            continue

    # ── 2) CASH match: amount + date + (atm/txn | branch | mobile) ─────────────
    for b in credits:
        if b["recon_status"]:
            continue
        cands = [l for l in acct_loads
                 if l["recon_status"] == "unmatched_load"
                 and amt_match(l["amount"], b["amount"])
                 and load_date(l) == b["txn_date"]]
        best = None; best_score = 0
        for l in cands:
            score = 0
            bref = _norm_ref(b.get("atm_ref"))
            if bref and (bref in _norm_ref(l.get("cdm_txn_number"))
                         or bref in _norm_ref(l.get("tid_chequeno"))
                         or _norm_ref(l.get("cdm_txn_number")) and _norm_ref(l.get("cdm_txn_number")) in bref):
                score += 2
            if b.get("branch") and _norm_ref(b.get("branch")) and (
                _norm_ref(b.get("branch")) == _norm_ref(l.get("branch_code"))
                or _norm_ref(b.get("branch")) in _norm_ref(l.get("branch_name"))):
                score += 1
            if b.get("mobile") and b.get("mobile") == l.get("cell_number"):
                score += 1
            if score > best_score:
                best_score = score; best = l
        if best is not None and best_score >= 1:
            mid = new_mid()
            b["recon_status"] = "matched_cash"; b["match_id"] = mid
            b["match_note"] = f"cash score={best_score}"
            best["recon_status"] = "matched_cash"; best["match_id"] = mid
            best["match_note"] = f"cash score={best_score}"

    # ── 3) Twice-credit: a 2nd bank credit equal to an already-matched one ─────
    seen = {}
    for b in credits:
        if b["recon_status"] in ("matched_online", "matched_cash"):
            key = (b["txn_date"], amt_key(b["amount"]), _norm_ref(b.get("utr")))
            seen.setdefault(key, []).append(b)
    for b in credits:
        if b["recon_status"]:
            continue
        key = (b["txn_date"], amt_key(b["amount"]), _norm_ref(b.get("utr")))
        if key in seen and seen[key]:
            b["recon_status"] = "twice_credit"
            b["match_id"] = seen[key][0]["match_id"]
            b["match_note"] = "duplicate of a matched credit"

    # ── 4) Remaining credits unmatched ────────────────────────────────────────
    for b in credits:
        if not b["recon_status"]:
            b["recon_status"] = "unmatched_bank"

    # ── 5) Debits: fee vs bank debit ──────────────────────────────────────────
    for b in debits:
        b["recon_status"] = "transaction_fee" if b["channel"] == "FEE" else "bank_debit"

    # ── Summary ───────────────────────────────────────────────────────────────
    def count(rows, status):
        return sum(1 for r in rows if r.get("recon_status") == status)

    summary = {
        "reco_acc_no":      reco_acc_no,
        "bank_rows":        len(bank_rows),
        "bank_credits":     len(credits),
        "bank_debits":      len(debits),
        "loads":            len(acct_loads),
        "matched_online":   count(bank_rows, "matched_online"),
        "matched_cash":     count(bank_rows, "matched_cash"),
        "twice_credit":     count(bank_rows, "twice_credit"),
        "wrong_amount":     count(bank_rows, "wrong_amount"),
        "unmatched_bank":   count(bank_rows, "unmatched_bank"),
        "unmatched_load":   count(acct_loads, "unmatched_load"),
        "transaction_fee":  count(bank_rows, "transaction_fee"),
        "bank_debit":       count(bank_rows, "bank_debit"),
    }
    matched = summary["matched_online"] + summary["matched_cash"]
    denom = summary["bank_credits"] or 1
    summary["match_rate"] = round(matched / denom * 100, 1)
    return {"bank_rows": bank_rows, "loads": acct_loads, "summary": summary}
