"""
Tests for the cross-product SRC (assign-src) disposition feature.

Load-bearing invariant: assigning an SRC tag sets recon_status='src_assigned'
(core / E-Value / BBPS) or persists an overlay row (SBI, whose P01–P04 results are
delete-and-recreated on every run — behavior-contract #17), and a reconciliation
RE-RUN must NOT clobber it. Also: open-items module rows must expose src_code.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from models.database import (
    Base, User, EvalueBankTxn, EvalueWalletLoad,
    BbpsBankTxn, BbpsInternal, SBIP02Result, SBISrcAssignment,
)

USER = User(id="u1", username="admin", role="admin", permissions="{}")
RD = "2026-06-25"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def _get(db, Model, _id):
    return db.query(Model).filter(Model.id == _id).first()


# ── E-Value ────────────────────────────────────────────────────────────────────
def test_evalue_assign_src_then_recon_preserves(db):
    from routes.evalue import assign_src, EvalueSRCIn, _run_one
    # one SRC-tagged row + one still-open row + a load, all on the same account
    db.add(EvalueBankTxn(id="eb1", reco_acc_no="ACC1", bank_name="SBI", txn_date=RD,
                         utr="U1", amount=5000.0, dr_cr="CR", recon_status="unmatched_bank"))
    db.add(EvalueBankTxn(id="eb2", reco_acc_no="ACC1", bank_name="SBI", txn_date=RD,
                         utr="U2", amount=250.0, dr_cr="CR", recon_status="unmatched_bank"))
    db.add(EvalueWalletLoad(id="el1", reco_acc_no="ACC1", transaction_date=RD,
                            amount=999.0, recon_status="unmatched_load"))
    db.commit()

    assign_src(EvalueSRCIn(id="eb1", side="bank", src_code="UNCLAIMED", src_note="legacy"),
               db=db, user=USER)
    row = _get(db, EvalueBankTxn, "eb1")
    assert row.recon_status == "src_assigned"
    assert row.src_code == "UNCLAIMED"

    # A recon re-run must leave the SRC-tagged row untouched (excluded from re-match).
    _run_one(db, "ACC1")
    row = _get(db, EvalueBankTxn, "eb1")
    assert row.recon_status == "src_assigned"
    assert row.src_code == "UNCLAIMED"
    assert row.src_note == "legacy"


def test_evalue_assign_src_rejects_bad_code(db):
    from routes.evalue import assign_src, EvalueSRCIn
    db.add(EvalueBankTxn(id="ebx", reco_acc_no="ACC1", txn_date=RD, amount=1.0,
                         dr_cr="CR", recon_status="unmatched_bank"))
    db.commit()
    with pytest.raises(HTTPException):
        assign_src(EvalueSRCIn(id="ebx", side="bank", src_code="NOT_A_CODE"), db=db, user=USER)


# ── BBPS ───────────────────────────────────────────────────────────────────────
def test_bbps_assign_src_then_recon_preserves(db):
    from routes.bbps import assign_src, BbpsSRCIn, run_recon
    db.add(BbpsBankTxn(id="bb1", provider="moneyart", client_ref="C1", amount=10.0,
                       status="Success", transaction_date=RD, recon_status="unmatched_bank"))
    db.add(BbpsInternal(id="bi1", eko_trxn_id="E1", provider="moneyart", amount=10.0,
                        status="Success", transaction_date=RD, recon_status="unmatched_internal"))
    db.commit()

    assign_src(BbpsSRCIn(id="bb1", side="bank", src_code="DUPLICATE"), db=db, user=USER)
    assert _get(db, BbpsBankTxn, "bb1").recon_status == "src_assigned"

    run_recon(db=db, user=USER)
    row = _get(db, BbpsBankTxn, "bb1")
    assert row.recon_status == "src_assigned"
    assert row.src_code == "DUPLICATE"


# ── SBI overlay survives delete-and-recreate (behavior-contract #17) ────────────
def test_sbi_src_overlay_survives_rerun(db):
    from routes.sbi_kiosk import assign_src, SBISRCIn, _apply_src_assignments
    db.add(SBIP02Result(id="r1", recon_date=RD, reference_number="REF1", bank_type="CR",
                        match_status="Unmatched"))
    db.commit()

    assign_src(SBISRCIn(process="p02", result_id="r1", src_code="MISSING_TID"),
               db=db, current_user=USER)
    # the tag lives in an overlay table keyed by the BUSINESS key, not the result id
    assert db.query(SBISrcAssignment).count() == 1

    # simulate a re-run: delete the result row, recreate it with a NEW id (same key)
    db.query(SBIP02Result).filter(SBIP02Result.id == "r1").delete()
    db.add(SBIP02Result(id="r2", recon_date=RD, reference_number="REF1", bank_type="CR",
                        match_status="Unmatched"))
    db.commit()

    rows = [{"id": "r2", "recon_date": RD, "reference_number": "REF1", "bank_type": "CR",
             "match_status": "Unmatched"}]
    _apply_src_assignments(db, "p02", RD, rows)
    assert rows[0]["src_code"] == "MISSING_TID"   # overlaid onto the regenerated row


# ── Open-items module rows expose src_code ──────────────────────────────────────
def test_module_rows_expose_src_code(db):
    from routes.recon import _module_rows
    db.add(EvalueBankTxn(id="eb9", reco_acc_no="ACC1", txn_date=RD, utr="U9",
                         amount=7.0, dr_cr="CR", recon_status="src_assigned",
                         src_code="OTHER", src_note="n"))
    db.commit()
    rows = _module_rows("evalue", db=db)            # src_assigned is now in _MODULE_OPEN
    match = [r for r in rows if r["id"] == "eb9"]
    assert match, "src_assigned module row should appear in open items"
    assert match[0]["src_code"] == "OTHER"
