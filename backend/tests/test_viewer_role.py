"""
Dashboard-only "viewer" accounts: the server-side scope in core/auth.py must let
a viewer reach ONLY the executive analytics endpoint (+ minimal auth), and deny
everything else; non-viewers are never affected. Also: creating a viewer stores
no operational permissions.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, User
from core.auth import _enforce_viewer_scope
from routes.auth import create_user, UserCreate


def _req(path):
    return SimpleNamespace(url=SimpleNamespace(path=path))


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def test_viewer_allowed_paths():
    u = SimpleNamespace(role="viewer")
    for p in ("/api/reports/analytics", "/api/auth/me", "/api/auth/logout"):
        _enforce_viewer_scope(u, _req(p))   # must NOT raise


def test_viewer_denied_everything_else():
    u = SimpleNamespace(role="viewer")
    denied = [
        "/api/recon/open-items", "/api/auth/users", "/api/reports/export",
        "/api/reports/funds-position", "/api/reports/library", "/api/sbi/summary",
        "/api/evalue/run-recon", "/api/upload/file", "/api/audit/logs",
    ]
    for p in denied:
        with pytest.raises(HTTPException) as e:
            _enforce_viewer_scope(u, _req(p))
        assert e.value.status_code == 403


def test_viewer_fails_closed_without_a_path():
    with pytest.raises(HTTPException):
        _enforce_viewer_scope(SimpleNamespace(role="viewer"), None)


def test_non_viewers_are_never_restricted():
    for role in ("user", "admin", ""):
        u = SimpleNamespace(role=role)
        _enforce_viewer_scope(u, _req("/api/recon/open-items"))  # no raise
        _enforce_viewer_scope(u, None)                            # no raise


def test_create_viewer_stores_no_permissions(db):
    admin = SimpleNamespace(id="admin-id", role="admin")
    create_user(UserCreate(username="ceo", password="x", role="viewer"),
                current_user=admin, db=db)
    u = db.query(User).filter(User.username == "ceo").first()
    assert u.role == "viewer"
    assert json.loads(u.permissions) == {}          # dashboard-only, no ops perms


def test_create_normal_user_keeps_default_permissions(db):
    admin = SimpleNamespace(id="admin-id", role="admin")
    create_user(UserCreate(username="op", password="x", role="user"),
                current_user=admin, db=db)
    u = db.query(User).filter(User.username == "op").first()
    assert json.loads(u.permissions).get("upload") is True   # unchanged behaviour
