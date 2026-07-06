"""
Cross-date RRN pass (QR T+1) — bank credit on day D, internal record on D+1:
per-(partner, recon_date) matching never sees the pair, this pass closes it on
tracking_number + amount (≤ ₹1) across dates.

Guards: unmatched-only, txn rows only, real dates only ('auto' excluded),
first-match-wins, match IDs = bank date via normal MAX+1 sequencing, scoped
partners only (CROSS_DATE_RRN_PARTNERS).
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Transaction, ReconStatus
from core.matching_engine import run_cross_date_rrn_match, CROSS_DATE_RRN_PARTNERS


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def _t(**kw):
    d = dict(partner="qr", row_type="txn", recon_status=ReconStatus.unmatched, amount=1977.0)
    d.update(kw)
    return Transaction(**d)


def test_qr_is_scoped():
    assert "qr" in CROSS_DATE_RRN_PARTNERS
    assert "fino" not in CROSS_DATE_RRN_PARTNERS   # DMT matches same-day; never wholesale


def test_matches_across_dates_on_rrn(db):
    db.add(_t(id="b1", side="bank", recon_date="2026-07-01", tracking_number="3567450341"))
    db.add(_t(id="i1", side="internal", recon_date="2026-07-02", tracking_number="3567450341",
              eko_tid="3567450390"))
    db.commit()

    r = run_cross_date_rrn_match("qr", db, "u1")
    assert r["cross_date_rrn_matched"] == 1
    b = db.query(Transaction).filter_by(id="b1").first()
    i = db.query(Transaction).filter_by(id="i1").first()
    assert b.recon_status == ReconStatus.matched and i.recon_status == ReconStatus.matched
    assert b.match_id == i.match_id and b.match_id
    assert "20260701" in b.match_id                 # ID minted on the BANK row's date
    assert b.matched_with_id == "i1" and i.matched_with_id == "b1"


def test_amount_gap_never_matches(db):
    db.add(_t(id="b1", side="bank", recon_date="2026-07-01",
              tracking_number="111", amount=2000.0))
    db.add(_t(id="i1", side="internal", recon_date="2026-07-02",
              tracking_number="111", amount=5000.0))
    db.commit()
    r = run_cross_date_rrn_match("qr", db, "u1")
    assert r["cross_date_rrn_matched"] == 0
    assert db.query(Transaction).filter_by(id="b1").first().recon_status == ReconStatus.unmatched


def test_only_unmatched_and_real_dates_participate(db):
    # already-matched internal must not be re-paired; 'auto' rows are excluded
    db.add(_t(id="b1", side="bank", recon_date="2026-07-01", tracking_number="222"))
    db.add(_t(id="i1", side="internal", recon_date="2026-07-02", tracking_number="222",
              recon_status=ReconStatus.matched, match_id="QRX-EXISTING"))
    db.add(_t(id="b2", side="bank", recon_date="auto", tracking_number="333"))
    db.add(_t(id="i2", side="internal", recon_date="2026-07-02", tracking_number="333"))
    db.commit()
    r = run_cross_date_rrn_match("qr", db, "u1")
    assert r["cross_date_rrn_matched"] == 0
    assert db.query(Transaction).filter_by(id="i1").first().match_id == "QRX-EXISTING"
    assert db.query(Transaction).filter_by(id="b2").first().recon_status == ReconStatus.unmatched


def test_first_match_wins_and_ids_unique(db):
    for n in range(3):
        db.add(_t(id=f"b{n}", side="bank", recon_date="2026-07-01",
                  tracking_number=f"rrn{n}", amount=100.0 + n))
        db.add(_t(id=f"i{n}", side="internal", recon_date="2026-07-02",
                  tracking_number=f"rrn{n}", amount=100.0 + n))
    db.commit()
    r = run_cross_date_rrn_match("qr", db, "u1")
    assert r["cross_date_rrn_matched"] == 3
    mids = [t.match_id for t in db.query(Transaction).filter(Transaction.side == "bank").all()]
    assert len(set(mids)) == 3                       # MAX+1 in-memory: no duplicate IDs


def test_failed_source_status_rows_never_pair(db):
    # A bank row whose SOURCE status is failed moved no money — even if it sits
    # recon_status=unmatched (pre-flagging data), the pass must skip it and pair
    # the retried Success copy instead.
    db.add(_t(id="bf", side="bank", recon_date="2026-06-12", tracking_number="777",
              amount=20000.0, status="Failed"))
    db.add(_t(id="bs", side="bank", recon_date="2026-06-12", tracking_number="777",
              amount=20000.0, status="Success"))
    db.add(_t(id="i1", side="internal", recon_date="2026-06-16", tracking_number="777",
              amount=20000.0, status="Success"))
    db.commit()
    r = run_cross_date_rrn_match("qr", db, "u1")
    assert r["cross_date_rrn_matched"] == 1
    assert db.query(Transaction).filter_by(id="bf").first().recon_status == ReconStatus.unmatched
    bs = db.query(Transaction).filter_by(id="bs").first()
    assert bs.recon_status == ReconStatus.matched and bs.matched_with_id == "i1"


def test_failed_internal_rows_never_pair(db):
    db.add(_t(id="b1", side="bank", recon_date="2026-07-01", tracking_number="888",
              status="Success"))
    db.add(_t(id="i1", side="internal", recon_date="2026-07-02", tracking_number="888",
              status="Refunded"))
    db.commit()
    r = run_cross_date_rrn_match("qr", db, "u1")
    assert r["cross_date_rrn_matched"] == 0


def test_other_partner_rows_untouched(db):
    db.add(_t(id="b1", side="bank", recon_date="2026-07-01", tracking_number="999",
              partner="fino"))
    db.add(_t(id="i1", side="internal", recon_date="2026-07-02", tracking_number="999",
              partner="fino"))
    db.commit()
    run_cross_date_rrn_match("qr", db, "u1")         # qr pass must not touch fino rows
    assert db.query(Transaction).filter_by(id="b1").first().recon_status == ReconStatus.unmatched
