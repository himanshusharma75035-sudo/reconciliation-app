"""
Tests for run_p02's settlement classification. A settlement debit (EKO DEDUCTION,
is_settlement) has NO counterpart in the BC transaction report — it reconciles in P01 by
(KO id, deduct date). run_p02 must therefore defer it to P01 exactly like the operator
report's reconcile() does, and NEVER label it a plain 'Unmatched' customer exception
(which inflated the dashboard/exec-digest "SBI Kiosk unmatched" figure). Customer rows are
unchanged.
"""
import types
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import (Base, SBIBankTransaction, SBITxnReport, SBIP01Result,
                             SBIP02Result, SBIKOLimits)
from routes.sbi_kiosk import run_p02

USER = types.SimpleNamespace(id="u1", username="tester", role="admin",
                             permissions="{}", is_api_key=False)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def _settlement(db, ko, deduct, posting, amt):
    bt = SBIBankTransaction(txn_date=posting, deduct_date=deduct, ko_id=ko, debit=amt, credit=0,
                            is_settlement=True, ref_number="",
                            description=f"TO TRANSFER-INB EKO DEDUCTION-{ko}.EKOSETTLEMENT")
    db.add(bt); db.commit()
    return bt.id


def _status(db, recon_date, bank_txn_id):
    run_p02(recon_date=recon_date, upload_date=None, db=db, current_user=USER)
    r = db.query(SBIP02Result).filter(SBIP02Result.bank_txn_id == bank_txn_id).first()
    return r.match_status if r else None


def test_settlement_matched_when_p01_matched_by_deduct_date(db):
    # deduct 25-Jul, bank posted 27-Jul (D+2); P01 matched it on the 25th.
    bid = _settlement(db, "1A850966", "2026-07-25", "2026-07-27", 23600)
    db.add(SBIP01Result(recon_date="2026-07-25", ko_id="1A850966", status="matched",
                        wallet_withdrawn=23600, bank_settled=23600,
                        deduct_date="2026-07-25", bank_txn_date="2026-07-27"))
    db.commit()
    assert _status(db, "2026-07-27", bid) == "Matched (Settlement)"


def test_settlement_pending_without_p01_or_kowdl(db):
    bid = _settlement(db, "1A850966", "2026-07-25", "2026-07-27", 23600)   # no P01, no same-day KO wdl
    assert _status(db, "2026-07-27", bid) == "Unmatched Settlement"


def test_settlement_matches_same_day_kowdl_without_p01(db):
    # legacy fallback preserved: a same-posting-day KO Withdrawal still matches with no P01 row.
    bid = _settlement(db, "1A850966", "2026-07-27", "2026-07-27", 23600)
    db.add(SBIKOLimits(txn_date="2026-07-27", ko_id="1A850966", txn_type="KO Withdrawal", amount=23600))
    db.commit()
    assert _status(db, "2026-07-27", bid) == "Matched (Settlement)"


def test_p01_unmatched_does_not_falsely_match(db):
    bid = _settlement(db, "1A850966", "2026-07-25", "2026-07-27", 23600)
    db.add(SBIP01Result(recon_date="2026-07-25", ko_id="1A850966", status="unmatched",
                        wallet_withdrawn=23600, bank_settled=0))
    db.commit()
    assert _status(db, "2026-07-27", bid) == "Unmatched Settlement"


def test_never_plain_unmatched(db):
    # the whole point: a settlement debit is never the customer 'Unmatched' bucket.
    bid = _settlement(db, "1A850966", "2026-07-25", "2026-07-27", 23600)
    assert _status(db, "2026-07-27", bid) not in ("Unmatched", "Partial")


def test_customer_row_with_report_still_matches(db):
    ref = "12345678901234567890"
    bt = SBIBankTransaction(txn_date="2026-07-27", ref_number=ref, debit=1000, credit=0,
                            is_settlement=False, description=f"BY TRANSFER-{ref}")
    db.add(bt)
    db.add(SBITxnReport(txn_date="2026-07-27", reference_number=ref, amount=1000,
                        status="Success", txn_type="Withdrawal"))
    db.commit()
    assert _status(db, "2026-07-27", bt.id) == "Matched"


def test_customer_cash_deposit_stays_unmatched(db):
    # a genuine gap (cash in, no BC transaction-report entry) must STILL count as Unmatched.
    bt = SBIBankTransaction(txn_date="2026-07-27", ref_number="", debit=0, credit=290000,
                            is_settlement=False, description="CASH DEPOSIT-CASH DEPOSIT SELF--")
    db.add(bt); db.commit()
    assert _status(db, "2026-07-27", bt.id) == "Unmatched"
