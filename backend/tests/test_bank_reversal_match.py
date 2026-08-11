"""
Characterization tests for run_bank_reversal_match — bank-side refund netting.

[Live case 2026-07-23 axis: an AePS-cashout refund showed on the bank statement as
a DR ₹9,400 out (narration = customer name, no Eko TID) + a CR ₹9,400 back (typed
settlement_credit). Neither matched internal (the internal legs self-matched as a
refund), so both bank legs sat 'unmatched'. This pass nets them → reversal_matched.
Signed off by finance-ops (himanshu) 2026-07-27.]

Invariants:
1. A DR + CR sharing tracking_number, opposite dr_cr, equal amount (±₹1), both
   unmatched → both become reversal_matched with a shared match_id.
2. row_type of the CR is irrelevant (a refund CR is often 'settlement_credit').
3. It ONLY touches 'unmatched' rows — matched / manual / src_assigned / fund_transfer
   rows are never disturbed (genuine settlement inflows are auto-closed fund_transfer).
4. It never pairs across different trackings, nor a lone DR / lone CR, nor an
   amount beyond tolerance, nor a null/placeholder tracking, nor cross-date legs.
5. Idempotent.
6. Guard A — defers entirely until the internal side for the date exists (so genuine
   bank↔internal matching had its chance; prevents bank-first premature netting).
7. Guard B — never nets a tracking that an OPEN (unmatched) internal row still carries
   on ANY date (that debit may still belong to the internal ledger cross-date).
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Transaction, ReconStatus
from core.matching_engine import run_bank_reversal_match

DATE = "2026-07-23"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield s
    s.close()


def mk(db, side="bank", partner="axis", recon_date=DATE, row_type="txn",
       recon_status=ReconStatus.unmatched, **kw):
    t = Transaction(partner=partner, side=side, recon_date=recon_date,
                    row_type=row_type, recon_status=recon_status, **kw)
    db.add(t)
    return t


def _anchor(db, recon_date=DATE):
    """Satisfy Guard A: an internal side exists for the date. Unique tracking +
    internal_matched so it never trips Guard B or gets counted as a bank row."""
    return mk(db, side="internal", tracking_number="ANCHORINT", amount=1.0, dr_cr="DR",
              recon_date=recon_date, recon_status=ReconStatus.internal_matched)


def test_pairs_refund_round_trip_regardless_of_cr_row_type(db):
    # The exact live shape: DR txn (no eko_tid) + CR settlement_credit, same tracking.
    _anchor(db)
    dr = mk(db, tracking_number="620408663716", utr_number="620408663716",
            amount=9400.0, dr_cr="DR", row_type="txn")
    cr = mk(db, tracking_number="620408663716", eko_tid="916020056063",
            amount=9400.0, dr_cr="CR", row_type="settlement_credit")
    db.commit()
    out = run_bank_reversal_match("axis", DATE, db, "u1")
    assert out["bank_reversal_matched"] == 1
    db.refresh(dr); db.refresh(cr)
    assert dr.recon_status == ReconStatus.reversal_matched
    assert cr.recon_status == ReconStatus.reversal_matched
    assert dr.match_id == cr.match_id and dr.match_id
    assert dr.matched_with_id == cr.id and cr.matched_with_id == dr.id


def test_within_one_rupee_tolerance(db):
    _anchor(db)
    mk(db, tracking_number="T1", amount=1000.0, dr_cr="DR")
    mk(db, tracking_number="T1", amount=1000.5, dr_cr="CR")
    db.commit()
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 1


def test_amount_beyond_tolerance_not_paired(db):
    _anchor(db)
    mk(db, tracking_number="T1", amount=1000.0, dr_cr="DR")
    mk(db, tracking_number="T1", amount=1002.0, dr_cr="CR")
    db.commit()
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 0


def test_different_tracking_not_paired(db):
    _anchor(db)
    mk(db, tracking_number="T1", amount=500.0, dr_cr="DR")
    mk(db, tracking_number="T2", amount=500.0, dr_cr="CR")
    db.commit()
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 0


def test_lone_debit_not_paired(db):
    _anchor(db)
    mk(db, tracking_number="T1", amount=500.0, dr_cr="DR")
    db.commit()
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 0


def test_never_touches_non_unmatched_rows(db):
    # a genuine settlement inflow is auto-closed fund_transfer — must stay untouched
    _anchor(db)
    dr = mk(db, tracking_number="T1", amount=500.0, dr_cr="DR",
            recon_status=ReconStatus.matched)
    cr = mk(db, tracking_number="T1", amount=500.0, dr_cr="CR", row_type="settlement_credit",
            recon_status=ReconStatus.fund_transfer)
    db.commit()
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 0
    db.refresh(dr); db.refresh(cr)
    assert dr.recon_status == ReconStatus.matched
    assert cr.recon_status == ReconStatus.fund_transfer


def test_null_placeholder_tracking_never_groups(db):
    _anchor(db)
    mk(db, tracking_number="\\N", amount=500.0, dr_cr="DR")
    mk(db, tracking_number="\\N", amount=500.0, dr_cr="CR")
    db.commit()
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 0


def test_only_same_recon_date(db):
    _anchor(db)
    mk(db, tracking_number="T1", amount=500.0, dr_cr="DR", recon_date=DATE)
    mk(db, tracking_number="T1", amount=500.0, dr_cr="CR", recon_date="2026-07-22")
    db.commit()
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 0


def test_idempotent(db):
    _anchor(db)
    mk(db, tracking_number="T1", amount=500.0, dr_cr="DR")
    mk(db, tracking_number="T1", amount=500.0, dr_cr="CR")
    db.commit()
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 1
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 0


def test_two_debits_one_credit_pairs_only_one(db):
    _anchor(db)
    mk(db, tracking_number="T1", amount=500.0, dr_cr="DR")
    mk(db, tracking_number="T1", amount=500.0, dr_cr="DR")
    mk(db, tracking_number="T1", amount=500.0, dr_cr="CR")
    db.commit()
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 1
    left = db.query(Transaction).filter(Transaction.side == "bank", Transaction.dr_cr == "DR",
                                        Transaction.recon_status == ReconStatus.unmatched).count()
    assert left == 1


# ── Guard A: no internal side for the date → defer (net nothing) ───────────────
def test_guard_a_defers_when_no_internal_side(db):
    # Same valid round trip, but NO internal row exists for the date at all.
    mk(db, tracking_number="T1", amount=500.0, dr_cr="DR")
    mk(db, tracking_number="T1", amount=500.0, dr_cr="CR")
    db.commit()
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 0
    # …and once the internal side lands, the same call nets it.
    _anchor(db); db.commit()
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 1


# ── Remove-from-Fund-Transfer action (Rajendra 2026-08) ───────────────────────
def test_remove_fund_transfer_reopens_and_nets(db):
    # The live shape: refund CR auto-closed 'fund_transfer' can't net its same-tracking open DR.
    # The operator action reopens it → run_bank_reversal_match pairs the round trip.
    from models.database import User
    from routes.recon import do_remove_fund_transfer, RemoveFundTransferRequest
    U = User(id="u1", username="raj", role="admin", permissions="{}")
    _anchor(db)
    dr = mk(db, tracking_number="622114772773", utr_number="622114772773",
            amount=12500.0, dr_cr="DR", row_type="txn")
    cr = mk(db, tracking_number="622114772773", eko_tid="916020056063", amount=12500.0,
            dr_cr="CR", row_type="settlement_credit", recon_status=ReconStatus.fund_transfer)
    db.commit()
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 0  # blocked
    out = do_remove_fund_transfer(RemoveFundTransferRequest(transaction_id=cr.id),
                                  db=db, current_user=U)
    assert out["matched"] is True
    db.refresh(dr); db.refresh(cr)
    assert cr.recon_status == ReconStatus.reversal_matched
    assert dr.recon_status == ReconStatus.reversal_matched
    assert dr.match_id == cr.match_id and dr.match_id


def test_remove_fund_transfer_rejects_non_fund_transfer(db):
    from fastapi import HTTPException
    from models.database import User
    from routes.recon import do_remove_fund_transfer, RemoveFundTransferRequest
    U = User(id="u1", username="raj", role="admin", permissions="{}")
    t = mk(db, tracking_number="X", amount=1.0, dr_cr="CR", recon_status=ReconStatus.unmatched)
    db.commit()
    with pytest.raises(HTTPException) as e:
        do_remove_fund_transfer(RemoveFundTransferRequest(transaction_id=t.id), db=db, current_user=U)
    assert e.value.status_code == 400


# ── Guard B: an OPEN internal row shares the tracking (even cross-date) → skip ──
def test_guard_b_skips_tracking_held_by_open_internal_row(db):
    _anchor(db)
    # a genuine internal Success on ANOTHER date carries the same tracking, still open
    mk(db, side="internal", tracking_number="T1", amount=500.0, dr_cr="DR",
       recon_date="2026-07-25", recon_status=ReconStatus.unmatched, status="Success")
    mk(db, tracking_number="T1", amount=500.0, dr_cr="DR")
    mk(db, tracking_number="T1", amount=500.0, dr_cr="CR")
    db.commit()
    # must NOT steal the debit — leave the round trip for genuine (cross-date) matching
    assert run_bank_reversal_match("axis", DATE, db, "u1")["bank_reversal_matched"] == 0
