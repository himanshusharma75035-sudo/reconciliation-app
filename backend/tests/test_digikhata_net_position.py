"""
Digikhata (PPI wallet) net-position pass.

Digikhata's internal ledger reconciles by netting; residual micro-rows left after the pair
passes are the expected net wallet movement, not open gaps. run_internal_match marks them
internal_matched (net position) — but ONLY for Digikhata (isolation). Other partners' residual
stays unmatched.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Transaction, ReconStatus
from core.matching_engine import run_internal_match

DATE = "2026-07-20"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def _int(db, partner, tid, amt, drcr="DR", status="Success"):
    db.add(Transaction(partner=partner, side="internal", recon_date=DATE, row_type="txn",
                       recon_status=ReconStatus.unmatched, eko_tid=tid, amount=amt,
                       dr_cr=drcr, status=status))
    db.commit()


def test_digikhata_residual_marked_net_position(db):
    _int(db, "digikhata", "T1", 4.24, "DR")            # unpaired micro-txns
    _int(db, "digikhata", "T2", 0.48, "CR")
    out = run_internal_match("digikhata", DATE, db, "u1")
    rows = db.query(Transaction).filter(Transaction.partner == "digikhata").all()
    assert all(r.recon_status == ReconStatus.internal_matched for r in rows)   # not open
    assert all("net position" in (r.src_note or "") for r in rows)
    assert out["net_position"] == 2


def test_digikhata_still_pairs_normally_first(db):
    # a genuine Success+Failed pair still self-matches via Pass 1 (not the net-position pass)
    _int(db, "digikhata", "P1", 100.0, "DR", status="Success")
    _int(db, "digikhata", "P1", 100.0, "CR", status="Failed")
    out = run_internal_match("digikhata", DATE, db, "u1")
    assert out["net_position"] == 0                    # nothing left over
    assert out["internal_matched"] == 1                # the real pair


def test_other_partner_residual_stays_open(db):
    _int(db, "fino", "T1", 4.24, "DR")
    out = run_internal_match("fino", DATE, db, "u1")
    r = db.query(Transaction).filter(Transaction.partner == "fino").first()
    assert r.recon_status == ReconStatus.unmatched     # isolation: untouched
    assert out.get("net_position", 0) == 0
