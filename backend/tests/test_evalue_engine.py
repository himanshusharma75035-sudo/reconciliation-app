"""
Tests for the E-Value reconciliation engine (core/evalue_engine.py).

Run from backend/:  pytest -q tests/test_evalue_engine.py
The bank-statement parser tests are skipped automatically if the sample files
aren't present, so the suite still runs in CI without them.
"""
import os
import pytest

from core import evalue_engine as E

SAMPLE_DIR = os.environ.get(
    "EVALUE_SAMPLE_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "..",
                 "sample files", "E value", "sample Bank statements"),
)


def _row(**kw):
    base = dict(dr_cr="CR", amount=0.0, txn_date="2025-11-19", utr="", channel="OTHER",
                description="", atm_ref="", branch="", mobile="")
    base.update(kw)
    return base


def _load(**kw):
    base = dict(reco_acc_no="SBI-4393", amount=0.0, transaction_date="2025-11-19",
                status="Success", dr_cr="CR", utr_number="", tid_chequeno="",
                cdm_txn_number="", cdm_branch="", branch_name="", branch_code="",
                cell_number="", remarks="", comments="", mode="")
    base.update(kw)
    return base


# ── UTR fuzzy matching ────────────────────────────────────────────────────────
def test_utr_match_prefix_dropped():
    assert E._utr_match("HDFCR52025111985035694", "R52025111985035694")

def test_utr_match_substring():
    # bank narration carries the channel prefix; internal stores the bare ref
    assert E._utr_match("IMPS532311899877", "532311899877")

def test_utr_match_rejects_unrelated():
    assert not E._utr_match("HDFCR52025111985035694", "ZZZ000111222")


# ── Online matching ───────────────────────────────────────────────────────────
def test_online_match_exact():
    bank = [_row(amount=5000.0, utr="HDFCR5ABC1234567890", channel="RTGS")]
    loads = [_load(reco_acc_no="SBI-9460", amount=5000.0, utr_number="R5ABC1234567890")]
    res = E.reconcile(bank, loads, "SBI-9460")
    assert res["summary"]["matched_online"] == 1

def test_utr_match_amount_differs_now_unmatched():
    # Rajendra 2026-07-14: an identifier match whose AMOUNT differs is left OPEN,
    # never linked as wrong_amount (the auto-matcher no longer creates wrong_amount).
    bank = [_row(amount=5000.0, utr="HDFCR5ABC1234567890", channel="RTGS")]
    loads = [_load(reco_acc_no="SBI-9460", amount=4000.0, utr_number="R5ABC1234567890")]
    res = E.reconcile(bank, loads, "SBI-9460")
    assert res["summary"]["wrong_amount"] == 0
    assert res["summary"]["matched_online"] == 0
    assert res["summary"]["unmatched_bank"] == 1
    assert res["summary"]["unmatched_load"] == 1


def test_utr_amount_match_but_date_differs_now_unmatched():
    # amount matches but the DATE differs → left OPEN (strict amount AND date).
    bank = [_row(amount=5000.0, utr="HDFCR5ABC1234567890", channel="RTGS", txn_date="2025-11-19")]
    loads = [_load(reco_acc_no="SBI-9460", amount=5000.0, utr_number="R5ABC1234567890",
                   transaction_date="2025-11-20")]
    res = E.reconcile(bank, loads, "SBI-9460")
    assert res["summary"]["matched_online"] == 0
    assert res["summary"]["unmatched_bank"] == 1
    assert res["summary"]["unmatched_load"] == 1


def test_utr_amount_and_date_match_still_matches():
    bank = [_row(amount=5000.0, utr="HDFCR5ABC1234567890", channel="RTGS", txn_date="2025-11-19")]
    loads = [_load(reco_acc_no="SBI-9460", amount=5000.0, utr_number="R5ABC1234567890",
                   transaction_date="2025-11-19")]
    res = E.reconcile(bank, loads, "SBI-9460")
    assert res["summary"]["matched_online"] == 1


# ── Tolerance ─────────────────────────────────────────────────────────────────
def test_tolerance_exact_vs_within():
    bank = [_row(amount=1000.0, utr="HDFCR5XYZ1234567890", channel="RTGS")]
    loads = [_load(reco_acc_no="SBI-9460", amount=1001.0, utr_number="R5XYZ1234567890")]
    assert E.reconcile([dict(bank[0])], [dict(loads[0])], "SBI-9460")["summary"]["matched_online"] == 0
    assert E.reconcile([dict(bank[0])], [dict(loads[0])], "SBI-9460", amount_tol=2.0)["summary"]["matched_online"] == 1


# ── Cash matching ─────────────────────────────────────────────────────────────
def test_cash_match_on_amount_date_and_branch():
    bank = [_row(amount=49500.0, channel="CDM", atm_ref="9993998400", branch="4292",
                 description="CSH DEP (CDM)-9993998400 8574--")]
    loads = [_load(reco_acc_no="SBI-4393", amount=49500.0, branch_code="4292",
                   cdm_txn_number="9993998400", remarks="cash")]
    res = E.reconcile(bank, loads, "SBI-4393")
    assert res["summary"]["matched_cash"] == 1


# ── Twice-credit ──────────────────────────────────────────────────────────────
def test_twice_credit_detection():
    bank = [_row(amount=100.0, utr="HDFCR5DUP1234567890", channel="RTGS"),
            _row(amount=100.0, utr="HDFCR5DUP1234567890", channel="RTGS")]
    loads = [_load(reco_acc_no="SBI-9460", amount=100.0, utr_number="R5DUP1234567890")]
    res = E.reconcile(bank, loads, "SBI-9460")
    assert res["summary"]["matched_online"] == 1
    assert res["summary"]["twice_credit"] == 1


# ── Source alias (Axis CDM) ───────────────────────────────────────────────────
def test_axis_cdm_alias():
    keys = E._acct_aliases("AXIS-9050")
    assert "AXISCDM" in keys


# ── Debit classification ──────────────────────────────────────────────────────
def test_debit_fee_vs_bank_debit():
    bank = [_row(dr_cr="DR", amount=50.0, description="MAB CHARGES"),
            _row(dr_cr="DR", amount=20.0, description="NEFT OUTWARD TRANSFER")]
    res = E.reconcile(bank, [], "SBI-4393")
    s = res["summary"]
    assert s["transaction_fee"] + s["bank_debit"] == 2


# ── Bank statement parsers (skipped if samples absent) ────────────────────────
# Fixture filenames are synthetic placeholders; drop your own bank-statement
# samples under SAMPLE_DIR with these names (or point EVALUE_SAMPLE_DIR at them).
# Tests skip automatically when a sample is absent, so CI is unaffected.
PARSERS = [
    ("AXIS BANK", "Axis Bank/axis-sample.csv"),
    ("BANK OF INDIA", "bank of india/boi-sample.xls"),
    ("IDFC BANK", "IDFC bank/idfc-sample.xlsx"),
    ("PUNJAB NATIONAL BANK", "Punjab national bank/pnb-sample.csv"),
    ("RBL BANK", "RBL bank/rbl-sample.xls"),
    ("STATE BANK OF INDIA", "State bank of india/sbi-sample.xls"),
    ("UNION BANK OF INDIA", "Union Bank of india/ubi-sample.xls"),
    ("IDBI BANK", "IDBI bank/idbi-sample.xls"),
]


@pytest.mark.parametrize("bank,rel", PARSERS)
def test_parser_extracts_rows(bank, rel):
    path = os.path.join(SAMPLE_DIR, rel)
    if not os.path.exists(path):
        pytest.skip(f"sample not present: {rel}")
    # SBI sample folder name vs parser key
    bname = "SBI" if bank == "STATE BANK OF INDIA" else bank
    rows = E.parse_bank_statement(bname, open(path, "rb").read(), os.path.basename(path))
    assert len(rows) > 0
    assert all("dr_cr" in r and "amount" in r and "txn_date" in r for r in rows)


def test_axis_parser_keeps_narration():
    path = os.path.join(SAMPLE_DIR, "Axis Bank/axis-sample.csv")
    if not os.path.exists(path):
        pytest.skip("axis sample not present")
    rows = E.parse_axis(open(path, "rb").read(), "axis.csv")
    cr = [r for r in rows if r["dr_cr"] == "CR"]
    assert cr and all(r["description"].strip() for r in cr)  # narration no longer lost
