"""
Tests for E-Value cross-account match integrity.

Two load-bearing invariants (both broken on 2026-07-02, found by finance ops):
1. EVX match IDs must NEVER be reused. The generator must derive from MAX(existing
   number)+1 across BOTH tables — a COUNT recycles IDs after delete-and-replace
   statement re-uploads (the EVX-00039..43 collision).
2. A per-account recon re-run must NOT dissolve one side of a cross-account pair
   (the counterpart lives in a different account), same protection as src_assigned.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, EvalueBankTxn, EvalueWalletLoad

RD = "2026-07-01"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def test_next_evx_number_uses_max_not_count(db):
    from routes.evalue import _next_evx_number
    # Two matched rows but with a HIGH id (as if earlier matches were deleted by a
    # statement re-upload). COUNT would say 3; MAX+1 must say 41.
    db.add(EvalueBankTxn(id="b1", reco_acc_no="A", txn_date=RD, amount=10.0,
                         dr_cr="CR", recon_status="matched_online", match_id="EVX-00040"))
    db.add(EvalueWalletLoad(id="l1", reco_acc_no="B", transaction_date=RD, amount=10.0,
                            recon_status="matched_online", match_id="EVX-00007"))
    db.commit()
    assert _next_evx_number(db) == 41


def test_next_evx_number_empty_db(db):
    from routes.evalue import _next_evx_number
    assert _next_evx_number(db) == 1


def test_cross_account_match_never_recycles_ids(db):
    from routes.evalue import _cross_account_reference_match
    # An existing (old) match holds EVX-00050; new cross-account pair must get 51+.
    db.add(EvalueBankTxn(id="old", reco_acc_no="UBI-1", txn_date="2026-06-14", amount=2000.0,
                         dr_cr="CR", recon_status="matched_online", match_id="EVX-00050"))
    # unmatched bank credit in account A, unmatched load in account B, same RRN
    db.add(EvalueBankTxn(id="nb", reco_acc_no="IDFC-1", txn_date=RD, amount=500.0,
                         dr_cr="CR", recon_status="unmatched_bank",
                         utr="UTIBR62026070187097411"))
    db.add(EvalueWalletLoad(id="nl", reco_acc_no="IDFC-2", transaction_date=RD, amount=500.0,
                            recon_status="unmatched_load",
                            tid_chequeno="UTIBR62026070187097411"))
    db.commit()

    res = _cross_account_reference_match(db)
    assert res["cross_account_matched"] == 1
    nb = db.query(EvalueBankTxn).filter_by(id="nb").first()
    nl = db.query(EvalueWalletLoad).filter_by(id="nl").first()
    assert nb.match_id == nl.match_id == "EVX-00051"     # max(50)+1, not count(1)+1
    assert nb.recon_status == nl.recon_status == "matched_online"
    # the old match is untouched
    assert db.query(EvalueBankTxn).filter_by(id="old").first().match_id == "EVX-00050"


def test_cross_account_match_rejects_amount_gap(db):
    from routes.evalue import _cross_account_reference_match
    # Same reference but amounts differ by > Rs 1 → must NOT match.
    db.add(EvalueBankTxn(id="nb", reco_acc_no="IDFC-1", txn_date=RD, amount=2000000.0,
                         dr_cr="CR", recon_status="unmatched_bank",
                         utr="UTIBR62026070187097411"))
    db.add(EvalueWalletLoad(id="nl", reco_acc_no="IDFC-2", transaction_date=RD, amount=2000.0,
                            recon_status="unmatched_load",
                            tid_chequeno="UTIBR62026070187097411"))
    db.commit()
    res = _cross_account_reference_match(db)
    assert res["cross_account_matched"] == 0
    assert db.query(EvalueBankTxn).filter_by(id="nb").first().recon_status == "unmatched_bank"
    assert db.query(EvalueWalletLoad).filter_by(id="nl").first().recon_status == "unmatched_load"


def test_rerun_preserves_cross_account_pairs(db):
    from routes.evalue import _run_one
    # A cross-account matched pair: load lives in ACC1, its bank side in OTHER.
    db.add(EvalueWalletLoad(id="xl", reco_acc_no="ACC1", transaction_date=RD, amount=700.0,
                            recon_status="matched_online", match_id="EVX-00009",
                            match_note="cross-account reference"))
    # plus ordinary rows so the account has something to re-match
    db.add(EvalueBankTxn(id="b1", reco_acc_no="ACC1", bank_name="SBI", txn_date=RD,
                         utr="U1", amount=100.0, dr_cr="CR", recon_status="unmatched_bank"))
    db.commit()

    _run_one(db, "ACC1")

    xl = db.query(EvalueWalletLoad).filter_by(id="xl").first()
    assert xl.recon_status == "matched_online"        # NOT reset by the re-run
    assert xl.match_id == "EVX-00009"
    assert xl.match_note == "cross-account reference"
