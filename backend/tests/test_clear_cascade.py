"""
Match-integrity cascade on deletion (post 2026-07-23 fix).

Invariants:
1. Module clear (_clear_module) deleting one leg of an E-Value/BBPS matched pair reverts
   every SURVIVING leg sharing that match_id to its side's unmatched status — including
   MANUAL (EVMAN-) and other paired-but-not-'matched%' statuses (wrong_amount etc.).
   [Live incident 2026-07-22: five matched_manual wallet loads stayed 'matched' forever
   after a bank-side clear, because manual dispositions survive re-runs by design.]
2. E-Value internal upsert (_upsert_wallet_loads) replacing a matched load resets the
   surviving bank leg.
3. Core _repair_orphaned_matches heals ALL paired statuses whose partner vanished,
   not just plain 'matched'.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import (Base, EvalueBankTxn, EvalueWalletLoad, Transaction,
                             generate_id)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def _pair(db, mid="EVMAN-TEST-000001", bank_status="matched_manual",
          load_status="matched_manual", date="2026-07-20"):
    b = EvalueBankTxn(id=generate_id(), reco_acc_no="TB-0001", txn_date=date,
                      amount=1000.0, dr_cr="CR", recon_status=bank_status, match_id=mid)
    l = EvalueWalletLoad(id=generate_id(), reco_acc_no="TB-0001", eko_trxn_id="e1",
                         amount=1000.0, transaction_date=date, value_date=date,
                         status="Success", recon_status=load_status, match_id=mid)
    db.add_all([b, l]); db.commit()
    return b, l


def test_module_clear_bank_side_reverts_manual_matched_load(db):
    from routes.upload import _clear_module
    _pair(db, mid="EVMAN-TB0001-AAAAAA")
    res = _clear_module("evalue", db, recon_date="2026-07-20", side="bank", user="t")
    db.commit()
    assert res["deleted"].get("evalue_bank_txns") == 1
    assert res["counterparts_unmatched"] == 1
    load = db.query(EvalueWalletLoad).one()
    assert load.recon_status == "unmatched_load"
    assert load.match_id is None
    assert "deleted" in (load.match_note or "")


def test_module_clear_reverts_wrong_amount_pairs_too(db):
    # paired statuses that do NOT start with 'matched' must also revert
    from routes.upload import _clear_module
    _pair(db, mid="EVMAN-TB0001-BBBBBB", bank_status="wrong_amount",
          load_status="wrong_amount")
    _clear_module("evalue", db, recon_date="2026-07-20", side="bank", user="t")
    db.commit()
    assert db.query(EvalueWalletLoad).one().recon_status == "unmatched_load"


def test_module_clear_untouched_date_keeps_matches(db):
    from routes.upload import _clear_module
    _pair(db, mid="EVMAN-TB0001-CCCCCC", date="2026-07-19")
    _clear_module("evalue", db, recon_date="2026-07-20", side="bank", user="t")
    db.commit()
    assert db.query(EvalueWalletLoad).one().recon_status == "matched_manual"
    assert db.query(EvalueBankTxn).count() == 1


def test_upsert_wallet_loads_resets_orphaned_bank_leg(db):
    from routes.evalue import _upsert_wallet_loads
    _pair(db, mid="EVMAN-TB0001-DDDDDD")
    _upsert_wallet_loads(db, [{"eko_trxn_id": "e1", "reco_acc_no": "TB-0001",
                               "amount": 1000.0, "transaction_date": "2026-07-20",
                               "status": "Success"}])
    db.commit()
    bank = db.query(EvalueBankTxn).one()
    assert bank.recon_status == "unmatched_bank"
    assert bank.match_id is None
    # the re-inserted load starts unmatched (recon will re-derive)
    load = db.query(EvalueWalletLoad).filter_by(eko_trxn_id="e1").one()
    assert (load.recon_status or "unmatched_load") != "matched_manual"


def test_repair_orphaned_matches_heals_manual_status(db):
    from core.matching_engine import _repair_orphaned_matches
    t = Transaction(id=generate_id(), partner="dmt", recon_date="2026-07-20",
                    side="bank", amount=500.0, recon_status="manual_matched",
                    match_id="MAN-1", matched_with_id="gone-row-id")
    db.add(t); db.commit()
    repaired = _repair_orphaned_matches("dmt", "2026-07-20", db)
    assert repaired == 1
    db.refresh(t)
    assert t.recon_status == "unmatched"
    assert t.matched_with_id is None
