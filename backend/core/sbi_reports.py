"""SBI Kiosk reference-based reconciliation + the two operator output workbooks.

This is the engine behind the two files finance ops reconcile against by hand:
  * Reconciliation_Report      — bank-statement-centric (which source product each
                                 bank row matched)
  * Source_Files_Match_Status  — source-file-centric (per product: matched / unmatched,
                                 split by transaction Status)

It is **read-only** over `sbi_bank_transactions` + `sbi_txn_reports`. It does NOT touch
the P01-P04 result tables or any matching-engine state — it is an additive reporting
layer. The matching rule is the finance-ops-approved one (see docs/sbi-kiosk.md § Resolved):

  Match the bank statement's 20-digit '61…' transaction number (extracted from the
  Description) against the pooled six source files' Reference Number, one-to-one,
  amount agreeing within ₹0.01. Non-20-digit / placeholder references are tagged
  Not Applicable (never Unmatched). Unmatched source rows are split by their source
  Status: Failure = expected (never posted to the bank), Success = a real gap to review.
"""
import io
import re
from collections import defaultdict

import pandas as pd

from models.database import SBIBankTransaction, SBITxnReport, SBIKOLimits, SBIP01Result

# ── matching constants ────────────────────────────────────────────────────────
TOL = 0.01                                   # SBI paisa tolerance (contract-wide)
_REF_RE = re.compile(r"^61\d{18}$")          # a valid SBI txn number: 20 digits, starts 61
_REF_SCAN = re.compile(r"61\d{18}")          # find one inside a bank Description
_PLACEHOLDER = {"", "-", "- / -", "-/-", "- /-", "-/ -", "n/a", "na"}

# canonical product names — these are the six source files + the labels the manual
# report uses. Ingestion stores the raw *filename* in source_file; we normalise it.
PRODUCTS = [
    "AEPS Withdrawal Transaction Report",
    "AePS Onus Deposit",
    "Deposit",
    "Money Transfer",
    "Other Transactions",
    "Withdrawal",
]


def canonical_product(source_file: str) -> str:
    """Map a raw upload filename to one of the six canonical product names.
    Order matters: the specific AEPS/Onus files must win over the generic
    Withdrawal/Deposit ones."""
    s = (source_file or "").lower()
    if "aeps" in s and "transaction" in s:
        return "AEPS Withdrawal Transaction Report"
    if "onus" in s and "deposit" in s:
        return "AePS Onus Deposit"
    if "money" in s and "transfer" in s:
        return "Money Transfer"
    if "other" in s:
        return "Other Transactions"
    if "deposit" in s:
        return "Deposit"
    if "withdrawal" in s:
        return "Withdrawal"
    return (source_file or "Unknown").strip() or "Unknown"


def _valid_ref(ref) -> bool:
    return bool(ref) and bool(_REF_RE.match(str(ref).strip()))


def _extract_bank_ref(b) -> str:
    """The bank row's 20-digit txn number. Ingestion already extracts one into
    ref_number; re-validate it, and fall back to scanning the Description so a
    liberal earlier extraction (which populated cash rows too) can't leak a bad ref."""
    r = (b.ref_number or "").strip()
    if _valid_ref(r):
        return r
    m = _REF_SCAN.search(b.description or "")
    return m.group(0) if m else ""


def _bank_amount(b) -> float:
    d = b.debit or 0
    c = b.credit or 0
    return d if d > 0 else c


def _dedup(rows, keyfn):
    """Keep the first row per key; drop later exact duplicates. Order-preserving."""
    seen = set()
    out = []
    for r in rows:
        k = keyfn(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# ── the reconciliation ─────────────────────────────────────────────────────────
def reconcile(db, recon_date: str) -> dict:
    """Reference-match the bank statement against the pooled six source files for one
    business date. Returns a rich dict consumed by both report builders. Read-only."""
    bank = (db.query(SBIBankTransaction)
              .filter(SBIBankTransaction.txn_date == recon_date)
              .order_by(SBIBankTransaction.created_at, SBIBankTransaction.id).all())
    src = (db.query(SBITxnReport)
             .filter(SBITxnReport.txn_date == recon_date)
             .order_by(SBITxnReport.source_file, SBITxnReport.txn_datetime, SBITxnReport.id).all())

    # Defensive exact-duplicate dedup (a file uploaded twice — e.g. a re-export whose
    # SHA differs from the first — doubles a product and floods it with phantom
    # 'Unmatched-Success' rows). Collapse only rows identical on the full business tuple,
    # so genuinely-distinct rows (incl. many sharing the '- / -' placeholder ref) survive.
    # Non-destructive: the DB is untouched; this only cleans the in-memory view.
    src = _dedup(src, lambda s: (canonical_product(s.source_file), (s.reference_number or "").strip(),
                                 round(s.amount or 0, 2), (s.status or "").strip(),
                                 (s.ko_id or "").strip(), (s.txn_datetime or "").strip(),
                                 (s.to_account or "").strip()))
    bank = _dedup(bank, lambda b: (b.txn_date, (b.ref_number or "").strip(),
                                   round(b.debit or 0, 2), round(b.credit or 0, 2),
                                   b.balance, (b.description or "").strip()))

    # index source rows by valid reference; carry per-row derived fields
    src_by_ref = defaultdict(list)
    src_meta = {}                                   # id(s) -> dict of derived state
    for s in src:
        ref = (s.reference_number or "").strip()
        valid = _valid_ref(ref)
        src_meta[id(s)] = {
            "product": canonical_product(s.source_file),
            "valid": valid,
            "match_bank_seq": None,
            "match_bank_date": None,
        }
        if valid:
            src_by_ref[ref].append(s)

    # duplicate txn numbers *within the bank statement* (same ref as both DR and CR)
    ref_bank = defaultdict(list)
    for b in bank:
        r = _extract_bank_ref(b)
        if r:
            ref_bank[r].append(b)
    dup_refs = {r: rows for r, rows in ref_bank.items()
                if len(rows) >= 2 and any((x.debit or 0) > 0 for x in rows)
                and any((x.credit or 0) > 0 for x in rows)}

    bank_recs = []
    used_src = set()
    n_with_ref = n_matched = n_ref_not_found = n_no_ref = n_amt_mismatch = n_reversal = 0

    for i, b in enumerate(bank, start=1):
        ref = _extract_bank_ref(b)
        amt = _bank_amount(b)
        matched = None
        amount_match = ""
        if ref:
            n_with_ref += 1
            cands = [s for s in src_by_ref.get(ref, []) if id(s) not in used_src]
            # prefer an amount-agreeing source row; else take any remaining same-ref row
            pick = next((s for s in cands if abs((s.amount or 0) - amt) <= TOL), None)
            if pick is None and cands:
                pick = cands[0]
            if pick is not None:
                matched = pick
                used_src.add(id(pick))
                m = src_meta[id(pick)]
                m["match_bank_seq"] = i
                m["match_bank_date"] = b.txn_date
                amount_match = "Yes" if abs((pick.amount or 0) - amt) <= TOL else "No"
                n_matched += 1
                if amount_match == "No":
                    n_amt_mismatch += 1
            elif ref in dup_refs:
                n_reversal += 1            # sibling leg of a DR+CR reversal — explained, not a gap
            else:
                n_ref_not_found += 1
        else:
            n_no_ref += 1

        status = ("Matched" if matched is not None
                  else ("Reversal" if ref in dup_refs
                        else ("Unmatched" if ref else "No Txn Number")))
        bank_recs.append({
            "Bank Stmt Row": i,
            "Txn Date": b.txn_date,
            "Value Date": b.value_date or "",
            "Description": b.description or "",
            "Branch Code": b.branch_code or "",
            "Debit": b.debit or 0,
            "Credit": b.credit or 0,
            "Balance": b.balance if b.balance is not None else "",
            "Extracted Txn No. (20-digit, starts 61)": ref,
            "Matched Source File": canonical_product(matched.source_file) if matched else "",
            "Matched Transaction Type": (matched.txn_type or "") if matched else "",
            "Source Amount": (matched.amount or 0) if matched else "",
            "Amount Match": amount_match,
            "Match Status": status,
            "_is_dup": ref in dup_refs,
        })

    # ── source-side classification ─────────────────────────────────────────────
    per_product = {p: {"total": 0, "matched": 0, "unmatched": 0,
                       "unmatched_success": 0, "unmatched_failure": 0, "not_applicable": 0}
                   for p in PRODUCTS}
    source_recs = []                                # every source row, annotated
    src_seq = defaultdict(int)                      # per-product running Sr. No. (proxy)
    for s in src:
        m = src_meta[id(s)]
        prod = m["product"]
        pp = per_product.setdefault(prod, {"total": 0, "matched": 0, "unmatched": 0,
                                           "unmatched_success": 0, "unmatched_failure": 0,
                                           "not_applicable": 0})
        src_seq[prod] += 1
        status = (s.status or "").strip()
        if not m["valid"]:
            match_status = "Not Applicable"          # GUID/placeholder ref — not bank-reconcilable
            pp["not_applicable"] += 1
        elif m["match_bank_seq"] is not None:
            match_status = "Matched"
            pp["matched"] += 1
        else:
            match_status = "Unmatched"
            pp["unmatched"] += 1
            if status.lower() == "success":
                pp["unmatched_success"] += 1
            else:
                pp["unmatched_failure"] += 1
        # 'total' counts only bank-reconcilable rows (exclude Not Applicable from denom)
        if m["valid"]:
            pp["total"] += 1
        source_recs.append({
            "product": prod,
            "Sr. No.": src_seq[prod],
            "KO ID": s.ko_id or "",
            "Transaction Date & Time": s.txn_datetime or "",
            "Reference Number": s.reference_number or "",
            "Type of Transaction": s.txn_type or "",
            "From Account": s.from_account or "",
            "To Account": s.to_account or "",
            "Amount": s.amount or 0,
            "Status": status,
            "Match Status": match_status,
            "Matched Bank Stmt Row": m["match_bank_seq"] if m["match_bank_seq"] else "",
            "Matched Bank Txn Date": m["match_bank_date"] or "",
        })

    totals = {
        "bank_total": len(bank),
        "bank_with_ref": n_with_ref,
        "bank_matched": n_matched,
        "bank_ref_not_found": n_ref_not_found,
        "bank_reversal_legs": n_reversal,
        "bank_no_ref": n_no_ref,
        "amount_mismatches": n_amt_mismatch,
        "source_total": sum(pp["total"] for pp in per_product.values()),
        "source_matched": sum(pp["matched"] for pp in per_product.values()),
        "source_unmatched": sum(pp["unmatched"] for pp in per_product.values()),
        "source_unmatched_success": sum(pp["unmatched_success"] for pp in per_product.values()),
        "source_unmatched_failure": sum(pp["unmatched_failure"] for pp in per_product.values()),
        "source_not_applicable": sum(pp["not_applicable"] for pp in per_product.values()),
    }

    # unmatched source rows, Success first (the review items float to the top)
    unmatched_source = [r for r in source_recs if r["Match Status"] == "Unmatched"]
    unmatched_source.sort(key=lambda r: (0 if (r["Status"] or "").lower() == "success" else 1,
                                         r["product"], r["Sr. No."]))
    # unmatched bank rows (had a ref but no source match)
    unmatched_bank = [r for r in bank_recs if r["Match Status"] == "Unmatched"]

    # duplicate groups for the bank sheet
    dup_rows = []
    for gno, (r, rows) in enumerate(sorted(dup_refs.items()), start=1):
        for b in rows:
            seq = next((br["Bank Stmt Row"] for br in bank_recs
                        if br["Extracted Txn No. (20-digit, starts 61)"] == r
                        and br["Description"] == (b.description or "")), "")
            dup_rows.append({
                "Group No.": gno,
                "Bank Stmt Row": seq,
                "Transaction Number": r,
                "Debit": b.debit or 0,
                "Credit": b.credit or 0,
                "Matched Source File": canonical_product(
                    next((s for s in src_by_ref.get(r, [])), None).source_file)
                    if src_by_ref.get(r) else "",
                "Description": b.description or "",
                "Remarks": "Same txn number posted as both Debit and Credit (reversal pattern)",
            })

    return {
        "recon_date": recon_date,
        "bank_recs": bank_recs,
        "source_recs": source_recs,
        "per_product": per_product,
        "totals": totals,
        "unmatched_source": unmatched_source,
        "unmatched_bank": unmatched_bank,
        "duplicates": dup_rows,
        "dup_ref_count": len(dup_refs),
    }


# ── workbook styling helpers ───────────────────────────────────────────────────
_HEADER_FILL = "094053"      # Eko teal
_GREEN = "C6E0B4"            # matched
_ORANGE = "F8CBAD"          # unmatched-Success → needs review
_RED = "FFC7CE"             # unmatched-Failure → expected
_GREY = "D9D9D9"            # not applicable


def _style_header(ws, ncols):
    from openpyxl.styles import PatternFill, Font
    fill = PatternFill("solid", fgColor=_HEADER_FILL)
    for cell in ws[1]:
        cell.fill = fill
        cell.font = Font(color="FFFFFF", bold=True)
    for i in range(1, ncols + 1):
        col = ws.cell(row=1, column=i)
        ws.column_dimensions[col.column_letter].width = max(11, min(46, len(str(col.value or "")) + 3))
    ws.freeze_panes = "A2"


def _write_sheet(writer, name, rows, cols):
    df = pd.DataFrame(rows, columns=cols)
    df.to_excel(writer, sheet_name=name[:31], index=False)
    ws = writer.sheets[name[:31]]
    _style_header(ws, len(cols))
    return ws


def _highlight_by_match(ws, rows, match_col_idx, status_col_idx):
    """Colour each data row by its Match Status / Status (green/orange/red/grey)."""
    from openpyxl.styles import PatternFill
    fills = {k: PatternFill("solid", fgColor=v) for k, v in
             {"g": _GREEN, "o": _ORANGE, "r": _RED, "x": _GREY}.items()}
    for i, r in enumerate(rows, start=2):
        ms = r.get("Match Status", "")
        st = (r.get("Status", "") or "").lower()
        key = None
        if ms == "Matched":
            key = "g"
        elif ms == "Not Applicable":
            key = "x"
        elif ms == "Unmatched":
            key = "o" if st == "success" else "r"
        if key:
            ws.cell(row=i, column=match_col_idx).fill = fills[key]


# ── Report A: Reconciliation_Report (bank-centric, 5 sheets) ────────────────────
def build_reconciliation_report(db, recon_date: str) -> io.BytesIO:
    from openpyxl.styles import Font
    R = reconcile(db, recon_date)
    t = R["totals"]
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        # Summary
        summ = [
            {"Metric": "Total Bank Statement Transactions", "Value": t["bank_total"]},
            {"Metric": "Bank Rows with a 20-digit '61..' Txn Number", "Value": t["bank_with_ref"]},
            {"Metric": "Bank Rows Matched to a Source File", "Value": t["bank_matched"]},
            {"Metric": "Bank Rows with Txn No. but NOT Found in Any Source File", "Value": t["bank_ref_not_found"]},
            {"Metric": "Bank Reversal Legs (DR+CR, same txn no.)", "Value": t["bank_reversal_legs"]},
            {"Metric": "Bank Rows with No Txn Number (cash / bank-only entries)", "Value": t["bank_no_ref"]},
            {"Metric": "Amount Mismatches Among Matched Rows", "Value": t["amount_mismatches"]},
            {"Metric": "", "Value": ""},
            {"Metric": "Total Records Across All 6 Source Files (excl. Not Applicable)", "Value": t["source_total"]},
            {"Metric": "Source Records NOT Found in Bank Statement", "Value": t["source_unmatched"]},
            {"Metric": "  — of which Status=Success (needs review)", "Value": t["source_unmatched_success"]},
            {"Metric": "  — of which Status=Failure (expected)", "Value": t["source_unmatched_failure"]},
            {"Metric": "Not-Applicable source rows (non-20-digit ref)", "Value": t["source_not_applicable"]},
            {"Metric": "Duplicate Txn Numbers in Bank Stmt (DR+CR)", "Value": R["dup_ref_count"]},
        ]
        _write_sheet(writer, "Summary", summ, ["Metric", "Value"])
        # Matched-by-source-file block appended below the metrics
        ws = writer.sheets["Summary"]
        start = len(summ) + 3
        ws.cell(row=start, column=1, value="Matched Count by Source File").font = \
            __import__("openpyxl").styles.Font(bold=True)
        hdr = ["Source File", "Matched Bank Rows", "Total Records in File", "Unmatched (mostly Failed)"]
        for j, h in enumerate(hdr, start=1):
            ws.cell(row=start + 1, column=j, value=h).font = __import__("openpyxl").styles.Font(bold=True)
        for k, p in enumerate(PRODUCTS, start=start + 2):
            pp = R["per_product"].get(p, {})
            ws.cell(row=k, column=1, value=p)
            ws.cell(row=k, column=2, value=pp.get("matched", 0))
            ws.cell(row=k, column=3, value=pp.get("total", 0))
            ws.cell(row=k, column=4, value=pp.get("unmatched", 0))

        # Bank Statement (Reconciled)
        bank_cols = ["Bank Stmt Row", "Txn Date", "Value Date", "Description", "Branch Code",
                     "Debit", "Credit", "Balance", "Extracted Txn No. (20-digit, starts 61)",
                     "Matched Source File", "Matched Transaction Type", "Source Amount",
                     "Amount Match", "Match Status"]
        wsb = _write_sheet(writer, "Bank Statement (Reconciled)", R["bank_recs"], bank_cols)
        _highlight_by_match(wsb, R["bank_recs"], len(bank_cols), None)

        # Unmatched Bank Entries
        _write_sheet(writer, "Unmatched Bank Entries",
                     [{"Bank Stmt Row": r["Bank Stmt Row"], "Txn Date": r["Txn Date"],
                       "Debit": r["Debit"], "Credit": r["Credit"], "Balance": r["Balance"],
                       "Extracted Txn No.": r["Extracted Txn No. (20-digit, starts 61)"],
                       "Description": r["Description"],
                       "Remarks": "Has a 20-digit txn no. but no matching source record"}
                      for r in R["unmatched_bank"]],
                     ["Bank Stmt Row", "Txn Date", "Debit", "Credit", "Balance",
                      "Extracted Txn No.", "Description", "Remarks"])

        # Unmatched Source Records (Success first)
        us_cols = ["Sr. No.", "KO ID", "Transaction Date & Time", "Reference Number",
                   "Type of Transaction", "From Account", "To Account", "Amount", "Status"]
        _write_sheet(writer, "Unmatched Source Records",
                     [{"Source File": r["product"], **{c: r[c] for c in us_cols}}
                      for r in R["unmatched_source"]],
                     ["Source File"] + us_cols)

        # Duplicate Txn in Bank Stmt
        _write_sheet(writer, "Duplicate Txn in Bank Stmt", R["duplicates"],
                     ["Group No.", "Bank Stmt Row", "Transaction Number", "Debit", "Credit",
                      "Matched Source File", "Description", "Remarks"])
    out.seek(0)
    return out


# ── Report B: Source_Files_Match_Status (source-centric, per-product sheets) ────
def build_source_match_report(db, recon_date: str) -> io.BytesIO:
    R = reconcile(db, recon_date)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        # Summary — per source file split by Success/Failure
        rows = []
        for p in PRODUCTS:
            pp = R["per_product"].get(p, {})
            rows.append({
                "Source File": p, "Total Records": pp.get("total", 0),
                "Matched": pp.get("matched", 0), "Unmatched": pp.get("unmatched", 0),
                "Unmatched - Success": pp.get("unmatched_success", 0),
                "Unmatched - Failure": pp.get("unmatched_failure", 0),
                "Not Applicable": pp.get("not_applicable", 0),
            })
        t = R["totals"]
        rows.append({"Source File": "TOTAL", "Total Records": t["source_total"],
                     "Matched": t["source_matched"], "Unmatched": t["source_unmatched"],
                     "Unmatched - Success": t["source_unmatched_success"],
                     "Unmatched - Failure": t["source_unmatched_failure"],
                     "Not Applicable": t["source_not_applicable"]})
        _write_sheet(writer, "Summary", rows,
                     ["Source File", "Total Records", "Matched", "Unmatched",
                      "Unmatched - Success", "Unmatched - Failure", "Not Applicable"])

        # Limit & Settlement — raw KO-limits for the date + our current P01 status.
        # NOTE: P01 settlement recon is a finance-ops "still open" item (docs/sbi-kiosk.md);
        # the Status shown is our existing P01 output, not a re-validated settlement match.
        ko = (db.query(SBIKOLimits).filter(SBIKOLimits.txn_date == recon_date)
                .order_by(SBIKOLimits.amount).all())
        p01 = {r.ko_id: r.status for r in
               db.query(SBIP01Result).filter(SBIP01Result.recon_date == recon_date).all()}
        _P01_LABEL = {"CREDITED": "Matched", "PENDING": "Pending",
                      "EXCESS": "Excess (bank only)", "PARTIAL": "Partial"}
        ls_rows = [{
            "Sr. No.": i, "Date of Transaction": r.txn_datetime or "",
            "Limit Configured By": r.limit_configured_by or "", "KO ID": r.ko_id or "",
            "Opening Limit": r.opening_limit if r.opening_limit is not None else "",
            "Type of Transaction": r.txn_type or "", "Amount": r.amount or 0,
            "Closing Limit": r.closing_limit if r.closing_limit is not None else "",
            "Settlement Status (P01)": _P01_LABEL.get(p01.get(r.ko_id, ""), p01.get(r.ko_id, "") or "—"),
        } for i, r in enumerate(ko, start=1)]
        _write_sheet(writer, "Limit & Settlement", ls_rows,
                     ["Sr. No.", "Date of Transaction", "Limit Configured By", "KO ID",
                      "Opening Limit", "Type of Transaction", "Amount", "Closing Limit",
                      "Settlement Status (P01)"])

        # one sheet per product
        by_prod = defaultdict(list)
        for r in R["source_recs"]:
            by_prod[r["product"]].append(r)
        prod_cols = ["Sr. No.", "KO ID", "Transaction Date & Time", "Reference Number",
                     "Type of Transaction", "From Account", "To Account", "Amount", "Status",
                     "Match Status", "Matched Bank Stmt Row", "Matched Bank Txn Date"]
        for p in PRODUCTS:
            recs = by_prod.get(p, [])
            ws = _write_sheet(writer, p, [{c: r[c] for c in prod_cols} for r in recs], prod_cols)
            _highlight_by_match(ws, recs, prod_cols.index("Match Status") + 1, None)
    out.seek(0)
    return out
