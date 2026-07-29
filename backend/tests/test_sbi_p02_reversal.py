"""
SBI Kiosk P02 reversals are Matched, not a separate 'Reversal' bucket.

Finance-ops rule: a transaction is either matched or unmatched. A reversal (the same 20-digit
reference posted as both a debit and a credit) cancels out — it IS reconciled. So run_p02 now
classifies both legs as 'Matched' (keeping reversal_type as metadata), and analytics buckets
'reversal' as matched, so the Kiosk match rate no longer excludes them into 'other'.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, User, SBIBankTransaction, SBIP02Result
from routes.sbi_kiosk import run_p02
from core.analytics import _bucket

USER = User(id="u1", username="raj", role="admin")
DATE = "2026-07-20"
REF = "62080837477100018743"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def test_reversal_pair_is_matched_not_reversal(db):
    # same reference as both a debit and a credit = a reversal that cancels out
    db.add(SBIBankTransaction(txn_date=DATE, debit=5000, credit=0, is_settlement=False,
                              ref_number=REF, description="BY TRANSFER"))
    db.add(SBIBankTransaction(txn_date=DATE, debit=0, credit=5000, is_settlement=False,
                              ref_number=REF, description="BY TRANSFER"))
    db.commit()
    out = run_p02(recon_date=DATE, db=db, current_user=USER)
    rows = db.query(SBIP02Result).filter(SBIP02Result.reference_number == REF).all()
    assert len(rows) == 2
    assert all(r.match_status == "Matched" for r in rows)               # not 'Reversal'
    assert {r.reversal_type for r in rows} == {"Reversal Debit", "Reversal Credit"}
    assert out["summary"]["Matched"] == 2
    assert out["summary"].get("Reversal") == 2                          # informational leg count
    assert "Reversal" not in {r.match_status for r in rows}


def test_analytics_buckets_reversal_as_matched():
    assert _bucket("Reversal") == "matched"
    assert _bucket("reversal") == "matched"
