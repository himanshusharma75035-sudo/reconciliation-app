"""
Login two-factor (email OTP) + trusted-device: the gate, the full flow, the
break-glass, and the critical invariant that a device-trust token can NEVER be
replayed as a session token. Plus @eko.co.in email enforcement on user create.
"""
import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, User, LoginOtp, generate_id
from core.auth import (hash_password, get_current_user, make_device_trust_token,
                       device_trust_valid, create_access_token)
import routes.auth as A
from routes.auth import login, verify_login_otp, create_user, VerifyOtpIn, UserCreate


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def _req():
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="1.2.3.4"))


@pytest.fixture(autouse=True)
def _clear_rl():
    A._RL_HITS.clear()
    yield
    A._RL_HITS.clear()


def _mk_user(db, username="alice", role="user", email="alice@eko.co.in", pw="pw123", active=True):
    u = User(id=generate_id(), username=username, role=role, email=email,
             hashed_password=hash_password(pw), is_active=active, permissions="{}")
    db.add(u); db.commit(); return u


def _form(username, password):
    return OAuth2PasswordRequestForm(grant_type="password", username=username, password=password, scope="")


def _admin():
    return SimpleNamespace(id="admin", role="admin")


# ── device-trust token must never be a session token ──────────────────────────
def test_device_trust_roundtrip():
    t = make_device_trust_token("alice", days=1)
    assert device_trust_valid(t, "alice")
    assert not device_trust_valid(t, "bob")
    assert not device_trust_valid("garbage", "alice")


def test_device_trust_rejected_as_session(db):
    _mk_user(db, "alice")
    t = make_device_trust_token("alice")
    with pytest.raises(HTTPException) as e:
        get_current_user(request=_req(), token=t, x_api_key=None, db=db)
    assert e.value.status_code == 401     # can NOT be used to authenticate


def test_normal_access_token_still_authenticates(db):
    _mk_user(db, "alice")
    t = create_access_token({"sub": "alice"})
    assert get_current_user(request=_req(), token=t, x_api_key=None, db=db).username == "alice"


# ── login gate ────────────────────────────────────────────────────────────────
def test_login_2fa_off_returns_token(db, monkeypatch):
    monkeypatch.setenv("LOGIN_2FA", "off")
    _mk_user(db, "alice", pw="pw123")
    res = login(request=_req(), form_data=_form("alice", "pw123"), x_device_trust=None, db=db)
    assert res["access_token"] and res["user"]["username"] == "alice"


def test_login_2fa_on_requires_otp(db, monkeypatch):
    monkeypatch.setenv("LOGIN_2FA", "on")
    monkeypatch.setattr(A, "_send_login_code", lambda to, code: True)
    _mk_user(db, "alice", pw="pw123")
    res = login(request=_req(), form_data=_form("alice", "pw123"), x_device_trust=None, db=db)
    assert res.get("otp_required") is True and "access_token" not in res
    assert res.get("pending") and "username" not in res     # username is inside the pending token
    assert db.query(LoginOtp).filter(LoginOtp.username == "alice").count() == 1


def test_login_viewer_exempt_from_2fa(db, monkeypatch):
    monkeypatch.setenv("LOGIN_2FA", "on")
    _mk_user(db, "vv", role="viewer", email=None, pw="pw123")
    res = login(request=_req(), form_data=_form("vv", "pw123"), x_device_trust=None, db=db)
    assert res["access_token"]


def test_login_trusted_device_skips_otp(db, monkeypatch):
    monkeypatch.setenv("LOGIN_2FA", "on")
    _mk_user(db, "alice", pw="pw123")
    trust = make_device_trust_token("alice")
    res = login(request=_req(), form_data=_form("alice", "pw123"), x_device_trust=trust, db=db)
    assert res["access_token"]


def test_login_bad_password(db, monkeypatch):
    monkeypatch.setenv("LOGIN_2FA", "on")
    _mk_user(db, "alice", pw="pw123")
    with pytest.raises(HTTPException) as e:
        login(request=_req(), form_data=_form("alice", "WRONG"), x_device_trust=None, db=db)
    assert e.value.status_code == 401


def test_login_no_email_clean_error_not_crash(db, monkeypatch):
    monkeypatch.setenv("LOGIN_2FA", "on")
    _mk_user(db, "alice", pw="pw123", email=None)
    with pytest.raises(HTTPException) as e:
        login(request=_req(), form_data=_form("alice", "pw123"), x_device_trust=None, db=db)
    assert e.value.status_code == 400


# ── full flow + verify-otp ────────────────────────────────────────────────────
def test_full_2fa_flow_and_device_trust(db, monkeypatch):
    monkeypatch.setenv("LOGIN_2FA", "on")
    cap = {}
    monkeypatch.setattr(A, "_send_login_code", lambda to, code: cap.update(code=code) or True)
    _mk_user(db, "alice", pw="pw123")
    r1 = login(request=_req(), form_data=_form("alice", "pw123"), x_device_trust=None, db=db)
    assert r1["otp_required"] and r1["pending"]
    pending = r1["pending"]
    wrong = "000000" if cap["code"] != "000000" else "111111"
    with pytest.raises(HTTPException) as e:
        verify_login_otp(request=_req(), body=VerifyOtpIn(pending=pending, code=wrong), db=db)
    assert e.value.status_code == 400
    r = verify_login_otp(request=_req(), body=VerifyOtpIn(pending=pending, code=cap["code"], remember_device=True), db=db)
    assert r["access_token"] and r["user"]["username"] == "alice" and r.get("device_trust")
    # the returned trust token then skips OTP next login
    assert login(request=_req(), form_data=_form("alice", "pw123"), x_device_trust=r["device_trust"], db=db)["access_token"]


def test_verify_otp_no_remember_no_trust_token(db, monkeypatch):
    monkeypatch.setenv("LOGIN_2FA", "on")
    cap = {}
    monkeypatch.setattr(A, "_send_login_code", lambda to, code: cap.update(code=code) or True)
    _mk_user(db, "alice", pw="pw123")
    p = login(request=_req(), form_data=_form("alice", "pw123"), x_device_trust=None, db=db)["pending"]
    r = verify_login_otp(request=_req(), body=VerifyOtpIn(pending=p, code=cap["code"], remember_device=False), db=db)
    assert "device_trust" not in r


def test_verify_otp_expired(db, monkeypatch):
    monkeypatch.setenv("LOGIN_2FA", "on")
    cap = {}
    monkeypatch.setattr(A, "_send_login_code", lambda to, code: cap.update(code=code) or True)
    _mk_user(db, "alice", pw="pw123")
    p = login(request=_req(), form_data=_form("alice", "pw123"), x_device_trust=None, db=db)["pending"]
    otp = db.query(LoginOtp).filter(LoginOtp.username == "alice").first()
    otp.expires_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=1); db.commit()
    with pytest.raises(HTTPException) as e:
        verify_login_otp(request=_req(), body=VerifyOtpIn(pending=p, code=cap["code"]), db=db)
    assert e.value.status_code == 400


def test_verify_otp_used_code_cannot_be_reused(db, monkeypatch):
    monkeypatch.setenv("LOGIN_2FA", "on")
    cap = {}
    monkeypatch.setattr(A, "_send_login_code", lambda to, code: cap.update(code=code) or True)
    _mk_user(db, "alice", pw="pw123")
    p = login(request=_req(), form_data=_form("alice", "pw123"), x_device_trust=None, db=db)["pending"]
    verify_login_otp(request=_req(), body=VerifyOtpIn(pending=p, code=cap["code"]), db=db)
    with pytest.raises(HTTPException) as e:
        verify_login_otp(request=_req(), body=VerifyOtpIn(pending=p, code=cap["code"]), db=db)
    assert e.value.status_code == 400


def test_verify_otp_requires_valid_pending(db, monkeypatch):
    # A username alone can't touch a victim's OTP — /verify-otp needs the pending
    # token that only /login (after the password) hands out.
    monkeypatch.setenv("LOGIN_2FA", "on")
    _mk_user(db, "alice")
    with pytest.raises(HTTPException) as e:
        verify_login_otp(request=_req(), body=VerifyOtpIn(pending="garbage", code="123456"), db=db)
    assert e.value.status_code == 401
    # a device-trust token is NOT a valid pending token
    with pytest.raises(HTTPException) as e:
        verify_login_otp(request=_req(),
                         body=VerifyOtpIn(pending=make_device_trust_token("alice"), code="123456"), db=db)
    assert e.value.status_code == 401


def test_pending_token_rejected_as_session(db):
    from core.auth import make_login_pending_token
    _mk_user(db, "alice")
    t = make_login_pending_token("alice")
    with pytest.raises(HTTPException) as e:
        get_current_user(request=_req(), token=t, x_api_key=None, db=db)
    assert e.value.status_code == 401     # a login_pending token can't authenticate


# ── @eko.co.in email enforcement on user create ───────────────────────────────
def test_create_user_requires_eko_email(db):
    for bad in ("bob@gmail.com", None, "x@eko.co.in.evil.com"):
        with pytest.raises(HTTPException) as e:
            create_user(UserCreate(username="bob", password="pw", role="user", email=bad),
                        current_user=_admin(), db=db)
        assert e.value.status_code == 400
    create_user(UserCreate(username="bob3", password="pw", role="user", email="Bob3@Eko.Co.In"),
                current_user=_admin(), db=db)
    assert db.query(User).filter(User.username == "bob3").first().email == "bob3@eko.co.in"


def test_create_viewer_no_email_ok(db):
    create_user(UserCreate(username="vv", password="pw", role="viewer", email=None),
                current_user=_admin(), db=db)
    assert db.query(User).filter(User.username == "vv").first() is not None
