"""
Executive analytics aggregator: correct status->bucket mapping, date filtering,
and cross-product rollup (core ledger + E-Value + SBI kiosk).
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import (Base, Transaction, ReconStatus,
                             EvalueBankTxn, EvalueWalletLoad, SBIP02Result, generate_id)
from core.analytics import build_analytics, _bucket


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def test_bucket_mapping():
    assert _bucket("matched") == "matched"
    assert _bucket("manual_matched") == "matched"
    assert _bucket("matched_online") == "matched"
    assert _bucket("unmatched") == "unmatched"
    assert _bucket("unmatched_bank") == "unmatched"
    assert _bucket("unmatched_load") == "unmatched"
    assert _bucket("amount_mismatch") == "mismatch"
    assert _bucket("wrong_amount") == "mismatch"
    assert _bucket("bank_debit") == "other"
    assert _bucket("duplicate") == "other"
    assert _bucket("Matched") == "matched"          # SBI capitalised form


def _core(db, partner, side, date, status, amount):
    db.add(Transaction(id=generate_id(), partner=partner, side=side, row_type="txn",
                       recon_date=date, recon_status=status, amount=amount))


def test_rollup_across_products_and_sides(db):
    # core ledger: qr — 2 matched (bank+internal), 1 unmatched bank
    _core(db, "qr", "bank", "2026-07-01", ReconStatus.matched, 100.0)
    _core(db, "qr", "internal", "2026-07-01", ReconStatus.matched, 100.0)
    _core(db, "qr", "bank", "2026-07-01", ReconStatus.unmatched, 50.0)
    # E-Value
    db.add(EvalueBankTxn(id=generate_id(), reco_acc_no="A", txn_date="2026-07-01",
                         recon_status="matched_online", amount=200.0, dr_cr="CR"))
    db.add(EvalueWalletLoad(id=generate_id(), reco_acc_no="A", transaction_date="2026-07-01",
                            recon_status="unmatched_load", amount=200.0))
    # SBI kiosk P02
    db.add(SBIP02Result(id=generate_id(), recon_date="2026-07-01", match_status="Matched", bank_amount=300.0))
    db.add(SBIP02Result(id=generate_id(), recon_date="2026-07-01", match_status="Unmatched", bank_amount=70.0))
    db.commit()

    a = build_analytics(db, date_from="2026-07-01", date_to="2026-07-01")
    t = a["totals"]
    assert t["matched"] == 4          # qr x2 + evalue x1 + sbi x1
    assert t["unmatched"] == 3        # qr x1 + evalue x1 + sbi x1
    assert t["matched_volume"] == 700.0
    assert t["open_volume"] == 320.0
    prods = {p["product"]: p for p in a["by_product"]}
    assert prods["qr"]["matched"] == 2 and prods["qr"]["unmatched"] == 1
    assert prods["evalue"]["matched"] == 1
    assert prods["sbi_kiosk"]["matched"] == 1
    # both-sides breakdown (ledger products: core + evalue)
    sides = {s["side"]: s for s in a["by_side"]}
    assert sides["bank"]["matched"] >= 1 and sides["internal"]["matched"] >= 1


def test_date_filter_excludes_out_of_range(db):
    _core(db, "qr", "bank", "2026-07-01", ReconStatus.matched, 10.0)
    _core(db, "qr", "bank", "2026-07-05", ReconStatus.matched, 10.0)
    db.commit()
    a = build_analytics(db, date_from="2026-07-02", date_to="2026-07-04")
    assert a["totals"]["matched"] == 0
    a2 = build_analytics(db, date_from="2026-07-01", date_to="2026-07-05")
    assert a2["totals"]["matched"] == 2
    assert len(a2["daily"]) == 2


def test_product_filter(db):
    _core(db, "qr", "bank", "2026-07-01", ReconStatus.matched, 10.0)
    _core(db, "fino", "bank", "2026-07-01", ReconStatus.matched, 20.0)
    db.commit()
    a = build_analytics(db, product="qr")
    assert [p["product"] for p in a["by_product"]] == ["qr"]
    assert a["totals"]["matched"] == 1


def test_auto_sentinel_dates_excluded(db):
    _core(db, "qr", "bank", "auto", ReconStatus.matched, 10.0)      # sentinel, not a real date
    _core(db, "qr", "bank", "2026-07-01", ReconStatus.matched, 10.0)
    db.commit()
    a = build_analytics(db)
    assert a["totals"]["matched"] == 1
    assert all(d["date"].startswith("2026") for d in a["daily"])
