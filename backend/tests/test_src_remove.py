"""
Remove-SRC across every product (himanshu 2026-08): wherever a row can be SRC-tagged there is
now a remove that reverts it to the EXACT status it held before the tag. Assign records
prev_recon_status; remove restores it (falling back to the product's unmatched status for rows
tagged before prev-status was recorded, or a corrupt self-referential prev). SBI's SRC is a
read-time overlay, so remove just deletes the overlay row. Core covers every core-ledger product
(DMT, AePS, IndoNepal, QR, PG, digikhata, …).
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import (Base, User, Transaction, EvalueBankTxn, EvalueWalletLoad,
                             BbpsBankTxn, BbpsInternal, SBIP02Result, SBISrcAssignment)

USER = User(id="u1", username="admin", role="admin", permissions="{}")
RD = "2026-06-25"


@pytest.fixture
def db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    s = sessionmaker(bind=e, autoflush=False, autocommit=False)()
    yield s
    s.close()


def _get(db, Model, _id):
    return db.query(Model).filter(Model.id == _id).first()


# ── CORE (covers DMT / AePS / IndoNepal / QR / PG / digikhata) ─────────────────
def test_core_remove_reverts_to_exact_prev(db):
    from core.matching_engine import assign_src
    from routes.recon import do_remove_src, SRCRemoveRequest
    db.add(Transaction(id="t1", partner="dmt", side="bank", recon_status="amount_mismatch", amount=100.0))
    db.commit()
    assign_src("t1", "OTHER", "note", db)
    t = _get(db, Transaction, "t1")
    assert t.recon_status == "src_assigned" and t.prev_recon_status == "amount_mismatch"
    do_remove_src(SRCRemoveRequest(transaction_id="t1"), db=db, current_user=USER)
    t = _get(db, Transaction, "t1")
    assert t.recon_status == "amount_mismatch"          # the EXACT prior status, not just 'unmatched'
    assert t.src_code is None and t.src_note is None and t.prev_recon_status is None


def test_core_remove_legacy_null_prev_falls_back_to_unmatched(db):
    from routes.recon import do_remove_src, SRCRemoveRequest
    # a row tagged the OLD way — src_assigned but prev_recon_status never recorded
    db.add(Transaction(id="t2", partner="aeps", side="bank", recon_status="src_assigned",
                       src_code="OTHER", prev_recon_status=None, amount=50.0))
    db.commit()
    do_remove_src(SRCRemoveRequest(transaction_id="t2"), db=db, current_user=USER)
    assert _get(db, Transaction, "t2").recon_status == "unmatched"


def test_core_remove_rejects_untagged(db):
    from fastapi import HTTPException
    from routes.recon import do_remove_src, SRCRemoveRequest
    db.add(Transaction(id="t3", side="bank", recon_status="unmatched", amount=1.0)); db.commit()
    with pytest.raises(HTTPException) as ex:
        do_remove_src(SRCRemoveRequest(transaction_id="t3"), db=db, current_user=USER)
    assert ex.value.status_code == 400


def test_core_remove_bulk(db):
    from routes.recon import (do_assign_src_bulk, BulkSRCRequest,
                              do_remove_src_bulk, BulkSRCRemoveRequest)
    for i in (1, 2):
        db.add(Transaction(id=f"c{i}", side="bank", recon_status="unmatched", amount=10.0))
    db.commit()
    do_assign_src_bulk(BulkSRCRequest(transaction_ids=["c1", "c2"], src_code="OTHER", src_note=""),
                       db=db, current_user=USER)
    assert all(_get(db, Transaction, x).recon_status == "src_assigned" for x in ("c1", "c2"))
    out = do_remove_src_bulk(BulkSRCRemoveRequest(transaction_ids=["c1", "c2"]), db=db, current_user=USER)
    assert out["reverted"] == 2
    assert all(_get(db, Transaction, x).recon_status == "unmatched" and _get(db, Transaction, x).src_code is None
               for x in ("c1", "c2"))


# ── E-VALUE (bank + wallet-load sides) ────────────────────────────────────────
@pytest.mark.parametrize("side,Model,unmatched", [
    ("bank", EvalueBankTxn, "unmatched_bank"),
    ("internal", EvalueWalletLoad, "unmatched_load"),
])
def test_evalue_remove_reverts(db, side, Model, unmatched):
    from routes.evalue import assign_src, remove_src, EvalueSRCIn, EvalueSRCRemoveIn
    if side == "bank":
        db.add(EvalueBankTxn(id="e1", reco_acc_no="A", txn_date=RD, utr="U", amount=5.0,
                             dr_cr="CR", recon_status=unmatched))
    else:
        db.add(EvalueWalletLoad(id="e1", reco_acc_no="A", transaction_date=RD, amount=5.0,
                                recon_status=unmatched))
    db.commit()
    assign_src(EvalueSRCIn(id="e1", side=side, src_code="OTHER", src_note="x"), db=db, user=USER)
    assert _get(db, Model, "e1").recon_status == "src_assigned"
    remove_src(EvalueSRCRemoveIn(id="e1", side=side), db=db, user=USER)
    r = _get(db, Model, "e1")
    assert r.recon_status == unmatched and r.src_code is None and r.prev_recon_status is None


def test_evalue_remove_bulk(db):
    from routes.evalue import assign_src, remove_src_bulk, EvalueSRCIn, EvalueBulkSRCRemoveIn
    for i in (1, 2):
        db.add(EvalueBankTxn(id=f"eb{i}", reco_acc_no="A", txn_date=RD, amount=1.0,
                             dr_cr="CR", recon_status="unmatched_bank"))
    db.commit()
    for i in (1, 2):
        assign_src(EvalueSRCIn(id=f"eb{i}", side="bank", src_code="OTHER"), db=db, user=USER)
    out = remove_src_bulk(EvalueBulkSRCRemoveIn(ids=["eb1", "eb2"], side="bank"), db=db, user=USER)
    assert out["reverted"] == 2
    assert all(_get(db, EvalueBankTxn, f"eb{i}").recon_status == "unmatched_bank" for i in (1, 2))


# ── BBPS ──────────────────────────────────────────────────────────────────────
def test_bbps_remove_reverts(db):
    from routes.bbps import assign_src, remove_src, BbpsSRCIn, BbpsSRCRemoveIn
    db.add(BbpsBankTxn(id="bb1", provider="moneyart", client_ref="C1", amount=10.0,
                       status="Success", transaction_date=RD, recon_status="unmatched_bank"))
    db.commit()
    assign_src(BbpsSRCIn(id="bb1", side="bank", src_code="DUPLICATE"), db=db, user=USER)
    assert _get(db, BbpsBankTxn, "bb1").recon_status == "src_assigned"
    remove_src(BbpsSRCRemoveIn(id="bb1", side="bank"), db=db, user=USER)
    r = _get(db, BbpsBankTxn, "bb1")
    assert r.recon_status == "unmatched_bank" and r.src_code is None


# ── SBI (overlay delete) ──────────────────────────────────────────────────────
def test_sbi_remove_deletes_overlay(db):
    from routes.sbi_kiosk import assign_src, remove_src, SBISRCIn, SBISRCRemoveIn
    db.add(SBIP02Result(id="r1", recon_date=RD, reference_number="REF1", bank_type="CR",
                        match_status="Unmatched"))
    db.commit()
    assign_src(SBISRCIn(process="p02", result_id="r1", src_code="MISSING_TID"), db=db, current_user=USER)
    assert db.query(SBISrcAssignment).count() == 1
    remove_src(SBISRCRemoveIn(process="p02", result_id="r1"), db=db, current_user=USER)
    assert db.query(SBISrcAssignment).count() == 0    # overlay gone → row reverts to untagged
