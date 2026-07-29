"""
SRC reason-code catalog (Rajendra point 3): the hard-coded 9-code list became a
managed catalog (SrcCode) that admins / src_manage users edit from Configuration,
and every product's assign-src validates against it. These pin the catalog +
validation helper + create/toggle endpoints.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from models.database import Base, SrcCode, seed_src_codes, DEFAULT_SRC_CODES
from routes.recon import (get_active_src_codes, is_valid_src_code, _normalize_src_code,
                          create_src_code, toggle_src_code, src_codes_catalog, SrcCodeIn)

ADMIN = SimpleNamespace(id="u1", username="admin", role="admin")


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield s
    s.close()


def test_seed_populates_nine_codes(db):
    seed_src_codes(db)
    assert db.query(SrcCode).count() == len(DEFAULT_SRC_CODES) == 9
    assert set(get_active_src_codes(db)) == {c for c, _ in DEFAULT_SRC_CODES}


def test_seed_is_idempotent(db):
    seed_src_codes(db); seed_src_codes(db)
    assert db.query(SrcCode).count() == 9


def test_fallback_to_hardcoded_when_table_empty(db):
    # no seed run → get_active_src_codes falls back to the seed list (never empty)
    from routes.recon import SRC_CODES
    assert get_active_src_codes(db) == list(SRC_CODES)


def test_is_valid_src_code(db):
    seed_src_codes(db)
    assert is_valid_src_code(db, "UNCLAIMED") is True
    assert is_valid_src_code(db, "NOT_A_CODE") is False


def test_normalize_code():
    assert _normalize_src_code("  my new code! ") == "MY_NEW_CODE"
    assert _normalize_src_code("Bank-Charges") == "BANK_CHARGES"
    assert _normalize_src_code("!!!") == ""


def test_create_new_code(db):
    seed_src_codes(db)
    out = create_src_code(SrcCodeIn(code="chargeback", label="Chargeback"), db=db, current_user=ADMIN)
    assert out["code"] == "CHARGEBACK" and out["reactivated"] is False
    assert is_valid_src_code(db, "CHARGEBACK") is True


def test_create_duplicate_active_conflicts(db):
    seed_src_codes(db)
    with pytest.raises(HTTPException) as e:
        create_src_code(SrcCodeIn(code="UNCLAIMED"), db=db, current_user=ADMIN)
    assert e.value.status_code == 409


def test_deactivate_hides_from_active_but_keeps_row(db):
    seed_src_codes(db)
    row = db.query(SrcCode).filter(SrcCode.code == "OTHER").first()
    toggle_src_code(row.id, db=db, current_user=ADMIN)
    assert is_valid_src_code(db, "OTHER") is False           # hidden from dropdowns
    assert db.query(SrcCode).filter(SrcCode.code == "OTHER").first() is not None  # not deleted


def test_recreate_reactivates_deactivated(db):
    seed_src_codes(db)
    row = db.query(SrcCode).filter(SrcCode.code == "OTHER").first()
    toggle_src_code(row.id, db=db, current_user=ADMIN)
    out = create_src_code(SrcCodeIn(code="other"), db=db, current_user=ADMIN)
    assert out["reactivated"] is True
    assert is_valid_src_code(db, "OTHER") is True
    # still exactly one OTHER row (no duplicate)
    assert db.query(SrcCode).filter(SrcCode.code == "OTHER").count() == 1


def test_empty_code_rejected(db):
    with pytest.raises(HTTPException) as e:
        create_src_code(SrcCodeIn(code="!!!"), db=db, current_user=ADMIN)
    assert e.value.status_code == 400


def test_deactivate_all_returns_empty_not_defaults(db):
    # Regression (adversarial review): a SEEDED table with every code deactivated must
    # return [] — NOT silently re-enable the hard-coded defaults.
    seed_src_codes(db)
    for row in db.query(SrcCode).all():
        row.is_active = False
    db.commit()
    assert get_active_src_codes(db) == []
    assert is_valid_src_code(db, "OTHER") is False
    assert is_valid_src_code(db, "UNCLAIMED") is False


def test_active_codes_are_stably_ordered(db):
    seed_src_codes(db)
    a = get_active_src_codes(db)
    b = get_active_src_codes(db)
    assert a == b and len(a) == 9   # deterministic order across calls


def test_catalog_lists_active_and_inactive(db):
    seed_src_codes(db)
    row = db.query(SrcCode).filter(SrcCode.code == "OTHER").first()
    toggle_src_code(row.id, db=db, current_user=ADMIN)
    cat = src_codes_catalog(db=db, current_user=ADMIN)
    assert len(cat) == 9
    other = next(c for c in cat if c["code"] == "OTHER")
    assert other["is_active"] is False
