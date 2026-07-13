"""
The opt-in "attach executive dashboard" block for scheduled report emails:
must render email-safe HTML (KPIs + per-product rows + live link) and NEVER raise.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Transaction, ReconStatus, generate_id
from core.report_scheduler import _analytics_email_html, _fmt_inr_short


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def _txn(db, partner, side, date, status, amt):
    db.add(Transaction(id=generate_id(), partner=partner, side=side, row_type="txn",
                       recon_date=date, recon_status=status, amount=amt))


def test_fmt_inr_short():
    assert _fmt_inr_short(0) == "₹0"
    assert _fmt_inr_short(150000) == "₹1.50 L"
    assert _fmt_inr_short(25000000) == "₹2.50 Cr"


def test_dashboard_html_has_kpis_and_products(db):
    _txn(db, "qr", "bank", "2026-07-13", ReconStatus.matched, 1000.0)
    _txn(db, "qr", "internal", "2026-07-13", ReconStatus.matched, 1000.0)
    _txn(db, "axis", "bank", "2026-07-13", ReconStatus.unmatched, 500.0)
    db.commit()
    html = _analytics_email_html(db, "2026-07-13", "2026-07-13")
    assert "EXECUTIVE RECONCILIATION DASHBOARD" in html
    assert "Activity for 2026-07-13" in html
    assert "/exec" in html               # link to the live dashboard
    assert "QR Collection" in html       # product-group label rolled up
    assert "Match rate" in html
    # no raw None / template holes leaked
    assert "{" not in html and "}" not in html


def test_dashboard_html_ranged_window_label(db):
    _txn(db, "qr", "bank", "2026-07-10", ReconStatus.matched, 10.0)
    db.commit()
    html = _analytics_email_html(db, "2026-07-10", "2026-07-13")
    assert "2026-07-10 → 2026-07-13" in html


def test_dashboard_html_empty_window_is_safe(db):
    html = _analytics_email_html(db, "2026-01-01", "2026-01-01")
    assert "EXECUTIVE RECONCILIATION DASHBOARD" in html
    assert "No reconciliation activity" in html


def test_dashboard_html_never_raises(db):
    # A degenerate call must still return a string, never propagate an exception.
    assert isinstance(_analytics_email_html(db, None, None), str)
