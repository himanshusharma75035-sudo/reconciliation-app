"""
_unified_entries(sides=...) must build ONLY the requested side — the pair-picker shows one
side and discarded the other, so building both was ~half wasted work. This pins that the
single-side build is byte-identical to the corresponding subset of the both-sides build
(default sides=None), so get_unified and the all-entries report stay unchanged while the
picker gets to skip the other side's thousands of rows.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, SBIBankTransaction, SBITxnReport, SBIKOLimits
from routes.sbi_kiosk import _unified_entries

RD = "2026-06-25"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    # bank side: one settlement debit + one statement credit
    s.add(SBIBankTransaction(id="b1", txn_date=RD, debit=1000.0, credit=0, is_settlement=True,
                             ko_id="KO0000001", ref_number="", description="EKOSETTLEMENT KO0000001", deduct_date=RD))
    s.add(SBIBankTransaction(id="b2", txn_date=RD, debit=0, credit=500.0, is_settlement=False,
                             ko_id="KO0000002", ref_number="12345678901234567890", description="credit"))
    # data side: two txn reports + a KO withdrawal + a KO deposit
    s.add(SBITxnReport(id="t1", txn_date=RD, amount=500.0, reference_number="12345678901234567890",
                       ko_id="KO0000002", txn_type="Money Transfer", source_file="AEPS", status="Success"))
    s.add(SBITxnReport(id="t2", txn_date=RD, amount=250.0, reference_number="09876543210987654321",
                       ko_id="KO0000003", txn_type="AEPS Withdrawal", source_file="AEPS", status="Success"))
    s.add(SBIKOLimits(id="w1", txn_date=RD, txn_type="KO Withdrawal", ko_id="KO0000001", amount=1000.0))
    s.add(SBIKOLimits(id="d1", txn_date=RD, txn_type="KO Deposit", ko_id="KO0000004", amount=2000.0))
    s.commit()
    yield s
    s.close()


def test_single_side_build_equals_both_sides_subset(db):
    both = _unified_entries(db, RD, include_deposits=True)                       # sides=None
    bank = _unified_entries(db, RD, include_deposits=True, sides={"bank"})
    data = _unified_entries(db, RD, include_deposits=True, sides={"data"})

    # single-side builds carry only their side
    assert bank and all(e["side"] == "bank" for e in bank)
    assert data and all(e["side"] == "data" for e in data)

    # partition: the two single-side builds together == the both-sides build (no gain, no loss)
    assert len(bank) + len(data) == len(both)
    assert len(bank) == 2 and len(data) == 4        # 2 bank txns; 2 txn reports + KO wd + KO deposit

    # byte-identical: each single-side entry equals its counterpart in the both-sides build
    both_by_id = {(e["side"], e["id"]): e for e in both}
    for e in bank + data:
        assert both_by_id[(e["side"], e["id"])] == e


def test_sides_filter_skips_deposits_on_bank_request(db):
    bank = _unified_entries(db, RD, include_deposits=True, sides={"bank"})
    assert all(e["source"] != "KO Deposit" for e in bank)     # data-only rows never built
