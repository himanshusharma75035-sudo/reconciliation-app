"""
P01 settlement matcher — matched / unmatched only, per-withdrawal by amount + the
settlement's DEDUCT (business) date, not the bank posting date.
Finance-ops decision (Rajendra 2026-07-27).

Invariants:
1. A settlement posted a day (or two) LATER still matches its withdrawal, because we
   match on the deduct_date from the description — not the bank txn_date. (The old D+1
   bug read PENDING on day D + EXCESS on day D+1.)
2. Statuses are only 'matched' / 'unmatched' — never CREDITED/PENDING/PARTIAL/EXCESS.
3. A KO is 'matched' iff every withdrawal has a settlement of the same amount AND no
   settlement is left over. A split KO (part settled) → 'unmatched'.
4. Matching is per-withdrawal by exact amount (±0.01), so two equal withdrawals need
   two settlements.
5. Settlements with no deduct_date fall back to their posting date (nothing dropped).
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, User, SBIKOLimits, SBIBankTransaction, SBIP01Result
from routes.sbi_kiosk import run_p01

USER = User(id="u1", username="raj", role="admin", permissions="{}")
D = "2026-07-22"
D1 = "2026-07-23"   # next day (settlement posts here, deduct_date still D)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield s
    s.close()


def _wdl(db, ko, amount, date=D):
    db.add(SBIKOLimits(txn_date=date, txn_datetime=f"{date} 09:00", ko_id=ko,
                       txn_type="KO Withdrawal", amount=amount))


def _settle(db, ko, amount, deduct_date=D, posting_date=D):
    db.add(SBIBankTransaction(txn_date=posting_date, ko_id=ko, debit=amount,
                              is_settlement=True, deduct_date=deduct_date,
                              description="EKO DEDUCTION"))


def _status(db, ko, date=D):
    r = db.query(SBIP01Result).filter(SBIP01Result.recon_date == date,
                                      SBIP01Result.ko_id == ko).one()
    return r.status


def test_same_day_full_settlement_is_matched(db):
    _wdl(db, "KO1", 50000.0); _settle(db, "KO1", 50000.0); db.commit()
    out = run_p01(D, None, db, USER)
    assert out["summary"] == {"matched": 1, "unmatched": 0}
    assert _status(db, "KO1") == "matched"


def test_d1_settlement_still_matches_on_business_date(db):
    # withdrawal on D; settlement POSTED on D+1 but its description says D (deduct_date=D)
    _wdl(db, "KO1", 90000.0)
    _settle(db, "KO1", 90000.0, deduct_date=D, posting_date=D1)
    db.commit()
    out = run_p01(D, None, db, USER)
    assert out["summary"] == {"matched": 1, "unmatched": 0}
    r = db.query(SBIP01Result).filter(SBIP01Result.ko_id == "KO1").one()
    assert r.status == "matched"
    assert r.bank_txn_date == D1        # posted next day
    assert r.deduct_date == D           # reconciled on the business date


def test_split_ko_is_unmatched(db):
    # ₹47k settled, ₹56k never settled → the KO is unmatched (no PARTIAL)
    _wdl(db, "KO1", 47000.0); _wdl(db, "KO1", 56000.0)
    _settle(db, "KO1", 47000.0); db.commit()
    out = run_p01(D, None, db, USER)
    assert out["summary"] == {"matched": 0, "unmatched": 1}
    assert _status(db, "KO1") == "unmatched"


def test_two_equal_withdrawals_need_two_settlements(db):
    _wdl(db, "KO1", 50000.0); _wdl(db, "KO1", 50000.0)
    _settle(db, "KO1", 50000.0); db.commit()     # only one settlement
    out = run_p01(D, None, db, USER)
    assert _status(db, "KO1") == "unmatched"      # one withdrawal still open
    # now add the second settlement and re-run → matched
    _settle(db, "KO1", 50000.0); db.commit()
    run_p01(D, None, db, USER)
    assert _status(db, "KO1") == "matched"


def test_bank_settlement_with_no_withdrawal_is_unmatched(db):
    # missing KO Limits file: settlement exists, no withdrawal → unmatched (not EXCESS)
    _settle(db, "KO9", 20000.0); db.commit()
    out = run_p01(D, None, db, USER)
    assert out["summary"] == {"matched": 0, "unmatched": 1}
    assert _status(db, "KO9") == "unmatched"


def test_no_source_data_is_skipped_not_wiped(db):
    db.add(SBIP01Result(recon_date=D, ko_id="OLD", status="matched")); db.commit()
    out = run_p01(D, None, db, USER)
    assert out.get("skipped") is True
    assert db.query(SBIP01Result).filter(SBIP01Result.recon_date == D).count() == 1  # not deleted


def test_settlement_without_deduct_date_falls_back_to_posting_date(db):
    _wdl(db, "KO1", 30000.0)
    _settle(db, "KO1", 30000.0, deduct_date="", posting_date=D)   # no deduct date
    db.commit()
    out = run_p01(D, None, db, USER)
    assert _status(db, "KO1") == "matched"


def test_within_one_paisa_tolerance(db):
    _wdl(db, "KO1", 1000.00); _settle(db, "KO1", 1000.005); db.commit()
    run_p01(D, None, db, USER)
    assert _status(db, "KO1") == "matched"


def test_legacy_status_migration(db):
    # rows whose date can no longer be re-run must still relabel to the two-state model
    from models.database import migrate_sbi_p01_statuses
    for ko, st in [("A", "CREDITED"), ("B", "PENDING"), ("C", "PARTIAL"),
                   ("D", "EXCESS"), ("E", "matched")]:
        db.add(SBIP01Result(recon_date="2026-06-01", ko_id=ko, status=st))
    db.commit()
    migrate_sbi_p01_statuses(db)
    got = {r.ko_id: r.status for r in db.query(SBIP01Result).all()}
    assert got == {"A": "matched", "B": "unmatched", "C": "unmatched",
                   "D": "unmatched", "E": "matched"}
    migrate_sbi_p01_statuses(db)   # idempotent — no further change
    assert {r.status for r in db.query(SBIP01Result).all()} == {"matched", "unmatched"}
