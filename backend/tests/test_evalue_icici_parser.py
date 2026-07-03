"""
Characterization test for the ICICI 'Detailed Statement' (.xlsx) parser.
Built from a synthetic workbook that mirrors the real layout (header block,
10-column data section, footer legend) — real statements never enter the repo.
"""
import io

import openpyxl

from core.evalue_engine import parse_bank_statement, BANK_PARSERS


def _synthetic_icici() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet0"
    rows = [
        [],
        ["Detailed Statement"],
        ["Name:", "EKO INDIA FINANCIAL SERVICES", "", "Account Currency:", "INR"],
        ["A/C No:", "000705048580"],
        ["Transaction Period", "From 01/07/2026 To 03/07/2026"],
        ["Advanced Search"],
        ["Transaction type:", "DR"],
        [],
        ["S.N.", "Tran. Id", "Value Date", "Transaction Date", "Transaction Posted Date",
         "Cheque. No./Ref. No.", "Transaction Remarks", "Withdrawal Amt (INR)",
         "Deposit Amt (INR)", "Balance (INR)"],
        ["1", "S51619732", "01/Jul/2026", "01/Jul/2026", "01/07/2026 10:17:56 AM", "",
         "MMT/IMPS/618210850962/FINO6408/FINO0009002", "30,000.00", "", "1,12,177.20"],
        ["2", "S53044610", "01/Jul/2026", "01/Jul/2026", "01/07/2026 12:03:02 PM", "",
         "RTGS-SBINR12026070133651998-EKO INDIA", "", "3,50,000.00", "4,62,177.20"],
        ["3", "S54561784", "02/Jul/2026", "02/Jul/2026", "02/07/2026 01:56:02 PM", "",
         "MMT/IMPS/618212227779/EKOIND/AXIS413", "1,50,000.00", "", "3,12,177.20"],
        [],
        ["Legend:"],
        ["1. MMT - IMPS transactions"],
        ["30. CMS - Internet bulk payments"],
    ]
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_icici_registered():
    assert "ICICI BANK" in BANK_PARSERS


def test_icici_parse_shape_and_values():
    rows = parse_bank_statement("ICICI BANK", _synthetic_icici(), "DetailedStatement.xlsx")
    assert len(rows) == 3                                  # footer legend + header block skipped

    r0, r1, r2 = rows
    assert r0["txn_date"] == "2026-07-01" and r0["dr_cr"] == "DR"
    assert r0["amount"] == 30000.0 and r0["balance"] == 112177.20   # Indian commas parsed
    assert r0["ref_no"] == "S51619732"                     # Tran. Id fallback when no cheque no
    assert r0["channel"] == "DEBIT"     # engine convention: DR rows are DEBIT regardless of rail

    assert r1["dr_cr"] == "CR" and r1["amount"] == 350000.0
    assert "SBINR12026070133651998" in (r1["utr"] or "")   # UTR extracted from RTGS remark
    assert r1["channel"] == "RTGS"

    assert r2["txn_date"] == "2026-07-02" and r2["dr_cr"] == "DR" and r2["balance"] == 312177.20

    # balance chain is self-consistent (closing = prev + CR - DR)
    assert round(r0["balance"] + r1["amount"], 2) == r1["balance"]
    assert round(r1["balance"] - r2["amount"], 2) == r2["balance"]
