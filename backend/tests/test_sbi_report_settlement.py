"""
Tests for the SBI Reconciliation Report settlement classification (issue #1) and the bank
ref-column placeholder guard (issue #2).

Issue #1: a settlement debit posts D+1/D+2 after its wallet-deduct date, so its KO Withdrawal
lives on the DEDUCT date, not the report's posting date. The report must defer to P01 (which
matches on the deduct date) instead of re-deriving on the posting date — otherwise the same
settlement reads "Matched" in the P01 tab but "Unmatched Settlement" in the report.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, SBIBankTransaction, SBIP01Result, SBIKOLimits
from core.sbi_reports import reconcile
from routes.sbi_kiosk import _clean_refno


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def _settlement(db, ko, deduct, posting, amt):
    db.add(SBIBankTransaction(txn_date=posting, deduct_date=deduct, ko_id=ko, debit=amt, credit=0,
                              is_settlement=True, description=f"EKO DEDUCTION-{ko}.EKOSETTLEMENT"))
    db.commit()


def _status(db, recon_date):
    return reconcile(db, recon_date)["bank_recs"][0]["Match Status"]


def test_settlement_matched_when_p01_matched_by_deduct_date(db):
    # deduct 25-Jul, bank posted 27-Jul (D+2); P01 matched it on the 25th.
    _settlement(db, "1A850966", "2026-07-25", "2026-07-27", 23600)
    db.add(SBIP01Result(recon_date="2026-07-25", ko_id="1A850966", status="matched",
                        wallet_withdrawn=23600, bank_settled=23600,
                        deduct_date="2026-07-25", bank_txn_date="2026-07-27"))
    db.commit()
    row = reconcile(db, "2026-07-27")["bank_recs"][0]
    assert row["Match Status"] == "Matched (Settlement)"     # not "Unmatched Settlement"
    assert row["Matched Transaction Type"] == "KO Withdrawal"
    assert row["Amount Match"] == "Yes"


def test_settlement_unmatched_without_p01_or_kowdl(db):
    _settlement(db, "1A850966", "2026-07-25", "2026-07-27", 23600)   # no P01, no same-day KO wdl
    assert _status(db, "2026-07-27") == "Unmatched Settlement"


def test_settlement_still_matches_same_day_kowdl_without_p01(db):
    # legacy fallback preserved: a same-posting-day KO Withdrawal still matches with no P01 row.
    _settlement(db, "1A850966", "2026-07-27", "2026-07-27", 23600)
    db.add(SBIKOLimits(txn_date="2026-07-27", ko_id="1A850966", txn_type="KO Withdrawal", amount=23600))
    db.commit()
    assert _status(db, "2026-07-27") == "Matched (Settlement)"


def test_p01_unmatched_does_not_falsely_match(db):
    # P01 exists but UNMATCHED for that KO/date → report must not claim a settlement match.
    _settlement(db, "1A850966", "2026-07-25", "2026-07-27", 23600)
    db.add(SBIP01Result(recon_date="2026-07-25", ko_id="1A850966", status="unmatched",
                        wallet_withdrawn=23600, bank_settled=0))
    db.commit()
    assert _status(db, "2026-07-27") == "Unmatched Settlement"


def test_clean_refno_drops_placeholders_keeps_real():
    assert _clean_refno("- / -") == ""
    assert _clean_refno("  -/-  ") == ""
    assert _clean_refno("") == ""
    assert _clean_refno("N/A") == ""
    assert _clean_refno("123456") == "123456"    # a genuine cheque number is preserved
