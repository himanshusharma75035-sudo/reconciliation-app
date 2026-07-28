"""
Tests for the P02 dateless-report fix.

Some 'Deposit' transaction-report files ingest with no per-row date, so those rows never fall
into any recon date and their bank counterparts stay Unmatched despite an exact 20-digit
reference. _backfill_dateless_txn_dates heals them from the bank row carrying the same
(globally-unique) reference, so P02 then matches them.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, User, SBIBankTransaction, SBITxnReport, SBIP02Result
from routes.sbi_kiosk import _backfill_dateless_txn_dates, run_p02

USER = User(id="u1", username="raj", role="admin")
REF = "62080837477100018743"       # a valid 20-digit SBI reference


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def _bank(db, ref, date, amt):
    db.add(SBIBankTransaction(txn_date=date, debit=0, credit=amt, is_settlement=False,
                              ref_number=ref, description=f"BY TRANSFER-{ref}Deposit"))
    db.commit()


def _dateless_report(db, ref, amt, sf="Deposit 7 to 10.xls"):
    db.add(SBITxnReport(txn_date="", source_file=sf, reference_number=ref,
                        amount=amt, txn_type="Deposit", status="Success"))
    db.commit()


def test_backfill_heals_dateless_report_from_bank_ref(db):
    _bank(db, REF, "2026-07-08", 10000)
    _dateless_report(db, REF, 10000)
    healed = _backfill_dateless_txn_dates(db)
    assert healed == {"2026-07-08"}
    rpt = db.query(SBITxnReport).filter(SBITxnReport.reference_number == REF).first()
    assert rpt.txn_date == "2026-07-08"


def test_dateless_report_matches_in_p02_after_backfill(db):
    _bank(db, REF, "2026-07-08", 10000)
    _dateless_report(db, REF, 10000)
    _backfill_dateless_txn_dates(db)
    run_p02(recon_date="2026-07-08", db=db, current_user=USER)
    res = db.query(SBIP02Result).filter(SBIP02Result.reference_number == REF).first()
    assert res is not None and res.match_status == "Matched"


def test_backfill_ignores_report_with_no_bank_counterpart(db):
    _dateless_report(db, "99999999999999999999", 5000)   # ref has no bank row
    assert _backfill_dateless_txn_dates(db) == set()
    rpt = db.query(SBITxnReport).first()
    assert (rpt.txn_date or "") == ""                     # stays dateless, untouched


def test_backfill_idempotent(db):
    _bank(db, REF, "2026-07-08", 10000)
    _dateless_report(db, REF, 10000)
    assert _backfill_dateless_txn_dates(db) == {"2026-07-08"}
    assert _backfill_dateless_txn_dates(db) == set()      # already dated → nothing to do
