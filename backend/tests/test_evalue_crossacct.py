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


def test_manual_match_amount_guard(db):
    from routes.evalue import manual_match, ManualMatchIn
    from models.database import User
    user = User(id="u1", username="tester", role="admin", permissions="{}")
    db.add(EvalueBankTxn(id="b1", reco_acc_no="IDFC-4408", txn_date=RD, amount=200000.0,
                         dr_cr="CR", recon_status="unmatched_bank"))
    db.add(EvalueWalletLoad(id="l1", reco_acc_no="IDFC-1701", transaction_date=RD,
                            amount=1000000.0, recon_status="unmatched_load"))
    # and an equal-amount pair that must still match cleanly
    db.add(EvalueBankTxn(id="b2", reco_acc_no="A", txn_date=RD, amount=500.0,
                         dr_cr="CR", recon_status="unmatched_bank"))
    db.add(EvalueWalletLoad(id="l2", reco_acc_no="A", transaction_date=RD,
                            amount=500.0, recon_status="unmatched_load"))
    db.commit()

    # Rs 2L vs Rs 10L → linked but flagged, NEVER matched_manual
    res = manual_match(ManualMatchIn(bank_txn_id="b1", load_id="l1"), db=db, user=user)
    assert res["status"] == "wrong_amount"
    b = db.query(EvalueBankTxn).filter_by(id="b1").first()
    l = db.query(EvalueWalletLoad).filter_by(id="l1").first()
    assert b.recon_status == l.recon_status == "wrong_amount"
    assert b.match_id == l.match_id and b.match_id.startswith("EVMAN-")
    assert "200000" in b.match_note and "1000000" in b.match_note

    # equal amounts still produce a clean manual match
    res = manual_match(ManualMatchIn(bank_txn_id="b2", load_id="l2"), db=db, user=user)
    assert res["status"] == "matched_manual"
    assert db.query(EvalueBankTxn).filter_by(id="b2").first().recon_status == "matched_manual"


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


def test_rerun_preserves_manual_and_interbank_matches(db):
    from routes.evalue import _run_one
    # Manual (EVMAN-) and interbank (EVIBT-) dispositions must survive a re-run —
    # their counterparts live outside this account's re-match universe.
    db.add(EvalueBankTxn(id="bm", reco_acc_no="ACC1", bank_name="SBI", txn_date=RD,
                         amount=500.0, dr_cr="CR", recon_status="matched_manual",
                         match_id="EVMAN-ACC1-ABC123", match_note="manual match"))
    db.add(EvalueBankTxn(id="bi", reco_acc_no="ACC1", bank_name="SBI", txn_date=RD,
                         amount=900.0, dr_cr="CR", recon_status="interbank_matched",
                         match_id="EVIBT-ACC1-DEF456"))
    db.add(EvalueBankTxn(id="b1", reco_acc_no="ACC1", bank_name="SBI", txn_date=RD,
                         utr="U9", amount=100.0, dr_cr="CR", recon_status="unmatched_bank"))
    db.commit()

    _run_one(db, "ACC1")

    bm = db.query(EvalueBankTxn).filter_by(id="bm").first()
    bi = db.query(EvalueBankTxn).filter_by(id="bi").first()
    assert bm.recon_status == "matched_manual" and bm.match_id == "EVMAN-ACC1-ABC123"
    assert bi.recon_status == "interbank_matched" and bi.match_id == "EVIBT-ACC1-DEF456"


def test_auto_recon_after_upload_never_raises(db, monkeypatch):
    import routes.evalue as EVR

    def _boom(db_, acct):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(EVR, "_run_one", _boom)
    monkeypatch.setattr(EVR, "_cross_account_reference_match",
                        lambda db_: (_ for _ in ()).throw(RuntimeError("x")))
    out = EVR._auto_recon_after_upload(db, ["ACC1", "ACC2"])   # must not raise
    assert out["ACC1"].startswith("skipped:") and out["ACC2"].startswith("skipped:")
    assert str(out["cross_account"]).startswith("skipped:")


def test_auto_recon_after_upload_runs_accounts(db):
    from routes.evalue import _auto_recon_after_upload
    db.add(EvalueBankTxn(id="b1", reco_acc_no="ACC1", bank_name="SBI", txn_date=RD,
                         utr="U1", amount=250.0, dr_cr="CR", recon_status="unmatched_bank"))
    db.add(EvalueWalletLoad(id="l1", reco_acc_no="ACC1", transaction_date=RD, amount=250.0,
                            recon_status="unmatched_load", utr_number="U1"))
    db.commit()
    out = _auto_recon_after_upload(db, ["ACC1", "NO_SUCH_ACCT"])
    assert out["ACC1"] == "ok"
    assert "no bank statement" in out["NO_SUCH_ACCT"]          # reported, not raised
    assert out["cross_account"] == 0 or isinstance(out["cross_account"], int)
