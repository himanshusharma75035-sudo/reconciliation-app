"""
API-key authorization scoping (security fix).

Before: an API-key principal was synthesised from its admin creator and kept
role='admin', so require_permission / require_admin / check_product_access all
short-circuited on the admin role — a scoped key silently carried full admin
authority and its declared permissions were ignored.

After: a key's authority is its OWN scope. It is admin ONLY if its permissions
explicitly say '{"admin": true}'. These pin that contract; JWT users are unchanged.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.auth import is_admin_principal, require_permission, require_admin


def _jwt(role="user", perms=None):
    return SimpleNamespace(role=role, permissions=json.dumps(perms or {}), is_api_key=False)


def _key(perms=None):
    # A synthesised API-key principal: role stripped to 'user', flagged is_api_key.
    return SimpleNamespace(role="user", permissions=json.dumps(perms or {}), is_api_key=True)


# ── is_admin_principal ────────────────────────────────────────────────────────

def test_jwt_admin_is_admin():
    assert is_admin_principal(_jwt(role="admin")) is True

def test_jwt_user_is_not_admin():
    assert is_admin_principal(_jwt(role="user")) is False

def test_api_key_without_admin_scope_is_not_admin():
    assert is_admin_principal(_key({"upload": True, "reports": True})) is False

def test_api_key_with_admin_scope_is_admin():
    assert is_admin_principal(_key({"admin": True})) is True


# ── require_permission ────────────────────────────────────────────────────────

def _check_perm(user, perm):
    return require_permission(perm)(current_user=user)

def test_jwt_admin_bypasses_permission():
    assert _check_perm(_jwt(role="admin"), "logic_builder") is not None

def test_scoped_key_denied_permission_it_lacks():
    # THE FIX: a key without the permission is denied (previously bypassed via admin).
    with pytest.raises(HTTPException) as e:
        _check_perm(_key({"upload": True}), "logic_builder")
    assert e.value.status_code == 403

def test_scoped_key_allowed_permission_it_has():
    assert _check_perm(_key({"upload": True}), "upload") is not None

def test_admin_scoped_key_bypasses_permission():
    assert _check_perm(_key({"admin": True}), "logic_builder") is not None

def test_jwt_user_denied_permission_it_lacks():
    with pytest.raises(HTTPException) as e:
        _check_perm(_jwt(role="user", perms={"reports": True}), "clear_data")
    assert e.value.status_code == 403


# ── require_admin ─────────────────────────────────────────────────────────────

def test_jwt_admin_passes_require_admin():
    assert require_admin(current_user=_jwt(role="admin")) is not None

def test_scoped_key_denied_require_admin():
    # THE FIX: a scoped key can no longer reach admin-only endpoints.
    with pytest.raises(HTTPException) as e:
        require_admin(current_user=_key({"upload": True}))
    assert e.value.status_code == 403

def test_admin_scoped_key_passes_require_admin():
    assert require_admin(current_user=_key({"admin": True})) is not None

def test_jwt_user_denied_require_admin():
    with pytest.raises(HTTPException) as e:
        require_admin(current_user=_jwt(role="user"))
    assert e.value.status_code == 403
