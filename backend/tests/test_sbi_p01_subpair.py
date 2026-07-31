"""
P01 sub-pair matching (Rajendra 2026-07-31): P01 pairs a KO's withdrawals ↔ settlements 1:1 by
amount and records WHICH amounts paired (matched_amounts). A KO-day that is short by one amount is
now 'partial' — the clean pairs are matched, only the residual stays open — instead of the old
all-or-nothing 'unmatched' that tainted every row. The 'matched' condition is UNCHANGED (every
amount pairs), so fully-matched days are byte-identical (monotonic). Per-row resolution flows
consistently to the unified view, the pair-picker and P02's settlement deferral.
"""
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import (Base, User, SBIKOLimits, SBIBankTransaction, SBIP01Result, SBIP02Result)
from routes.sbi_kiosk import run_p01, run_p02, _unified_entries, manual_pair_open_items

USER = User(id="u1", username="admin", role="admin", permissions="{}")
RD = "2026-06-25"


@pytest.fixture
def db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    s = sessionmaker(bind=e, autoflush=False, autocommit=False)()
    def wdl(ko, amt):
        s.add(SBIKOLimits(txn_date=RD, txn_type="KO Withdrawal", ko_id=ko, amount=amt))
    def setl(ko, amt):
        s.add(SBIBankTransaction(txn_date=RD, deduct_date=RD, is_settlement=True, ko_id=ko,
                                 debit=amt, credit=0, description=f"EKO DEDUCTION-{ko}", ref_number=""))
    # KO A: partial — 48k/12k/180k pair, 8k withdrawal has no settlement
    for a in (48000, 12000, 180000, 8000): wdl("A", a)
    for a in (48000, 180000, 12000):       setl("A", a)
    # KO B: fully matched
    wdl("B", 100); setl("B", 100)
    # KO C: nothing paired
    wdl("C", 500)
    s.commit()
    yield s
    s.close()


def _p01(db, ko):
    return db.query(SBIP01Result).filter(SBIP01Result.recon_date == RD, SBIP01Result.ko_id == ko).one()


def test_engine_status_and_matched_amounts(db):
    run_p01(recon_date=RD, db=db, current_user=USER)
    a, b, c = _p01(db, "A"), _p01(db, "B"), _p01(db, "C")
    assert a.status == "partial" and json.loads(a.matched_amounts) == [12000.0, 48000.0, 180000.0]
    assert b.status == "matched" and json.loads(b.matched_amounts) == [100.0]   # monotonic: unchanged
    assert c.status == "unmatched" and json.loads(c.matched_amounts) == []


def test_unified_per_row_resolution(db):
    run_p01(recon_date=RD, db=db, current_user=USER)
    ents = _unified_entries(db, RD)
    wd = {(e["ko_csp"], e["amount"]): e["status"] for e in ents if e["source"] == "KO Withdrawal"}
    st = {(e["ko_csp"], e["amount"]): e["status"] for e in ents if e["source"] == "Bank Settlement"}
    # KO A: the three clean pairs are Matched on BOTH legs; only the 8k withdrawal stays open
    assert wd[("A", 48000)] == "Matched" and wd[("A", 12000)] == "Matched" and wd[("A", 180000)] == "Matched"
    assert wd[("A", 8000)] == "Unmatched"
    assert st[("A", 48000)] == "Matched" and st[("A", 180000)] == "Matched" and st[("A", 12000)] == "Matched"
    assert wd[("B", 100)] == "Matched" and st[("B", 100)] == "Matched"
    assert wd[("C", 500)] == "Unmatched"


def test_picker_excludes_matched_subpairs_keeps_residual(db):
    run_p01(recon_date=RD, db=db, current_user=USER)
    data = manual_pair_open_items(side="data", date_from=RD, date_to=RD, db=db, current_user=None)
    bank = manual_pair_open_items(side="bank", date_from=RD, date_to=RD, db=db, current_user=None)
    data_amts = {(i["ko_csp"], i["amount"]) for i in data["items"]}
    bank_amts = {(i["ko_csp"], i["amount"]) for i in bank["items"]}
    assert ("A", 8000) in data_amts and ("C", 500) in data_amts          # residuals stay open
    assert ("A", 48000) not in data_amts and ("A", 12000) not in data_amts  # clean pairs closed
    assert bank_amts.isdisjoint({("A", 48000), ("A", 12000), ("A", 180000)})  # settlements closed


def test_p02_settlement_deferral_covers_partial_day(db):
    run_p01(recon_date=RD, db=db, current_user=USER)
    run_p02(recon_date=RD, db=db, current_user=USER)
    # each partial-day paired settlement is 'Matched (Settlement)', not 'Unmatched Settlement'
    setl_status = [r.match_status for r in db.query(SBIP02Result).filter(
        SBIP02Result.recon_date == RD, SBIP02Result.bank_type == "DR").all()]
    assert setl_status and all(s == "Matched (Settlement)" for s in setl_status)


def test_matched_day_count_is_monotonic(db):
    # only KO B is fully matched; A is partial, C unmatched — the 'matched' set is exactly the
    # fully-paired days, same rule as before the change
    res = run_p01(recon_date=RD, db=db, current_user=USER)
    assert res["summary"] == {"matched": 1, "partial": 1, "unmatched": 1}
