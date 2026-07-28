"""
Tests for the raw-data download center (routes/downloads.py).

Verifies the catalog lists core-ledger partners + module products, the Excel export honours
the business-date range for both a core (Transaction) source and a module source, and that
every export writes an audit-log row.
"""
import io
import openpyxl
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, User, Transaction, SBIBankTransaction, AuditLog
from routes.downloads import download_catalog, download_export, _build_export

USER = User(id="u1", username="raj", role="admin")


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def _txn(db, partner, side, date, amount):
    db.add(Transaction(partner=partner, side=side, recon_date=date, amount=amount, row_type="txn"))
    db.commit()


def test_catalog_lists_core_partner_and_module(db):
    _txn(db, "fino", "bank", "2026-06-20", 100)
    _txn(db, "fino", "internal", "2026-06-20", 100)
    db.add(SBIBankTransaction(txn_date="2026-06-20", debit=50)); db.commit()
    out = download_catalog(db=db, current_user=USER)
    by_key = {p["key"]: p for p in out["products"]}
    assert "fino" in by_key and by_key["fino"]["group"] == "core"
    assert by_key["fino"]["sides"]["bank"]["count"] == 1
    assert "kiosk" in by_key and by_key["kiosk"]["group"] == "module"
    assert by_key["kiosk"]["sides"]["bank"]["count"] == 1


def _wb_rows(content):
    wb = openpyxl.load_workbook(io.BytesIO(content))
    ws = wb["Data"]
    return ws.max_row - 1   # minus header


def test_export_core_honours_date_range(db):
    for d, amt in [("2026-06-20", 100), ("2026-06-21", 200), ("2026-06-25", 300)]:
        _txn(db, "fino", "bank", d, amt)
    content, n = _build_export(db, "fino", "bank", "2026-06-20", "2026-06-21")
    assert n == 2 and _wb_rows(content) == 2


def test_export_module_source(db):
    db.add(SBIBankTransaction(txn_date="2026-06-20", debit=50, ref_number="X1"))
    db.add(SBIBankTransaction(txn_date="2026-06-21", debit=60, ref_number="X2"))
    db.commit()
    content, n = _build_export(db, "kiosk", "bank")   # no dates → everything
    assert n == 2 and _wb_rows(content) == 2


def test_export_writes_audit_row(db):
    _txn(db, "fino", "bank", "2026-06-20", 100)
    resp = download_export(product="fino", side="bank", date_from=None, date_to=None,
                           db=db, current_user=USER)
    assert resp.status_code == 200
    assert db.query(AuditLog).filter(AuditLog.action == "data_download").count() == 1


def test_bad_side_rejected(db):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _build_export(db, "fino", "sideways")
    assert e.value.status_code == 400
