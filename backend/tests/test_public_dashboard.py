"""
Passwordless @eko.co.in email-code access to the executive dashboard:
domain gate, code lifecycle (send / verify / wrong / expired / reuse / cooldown),
and that a verified code mints a dashboard-only VIEWER session.
"""
import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, DashboardOtp, User
import routes.public_dashboard as pd
from routes.public_dashboard import request_code, verify_code, EmailIn, VerifyIn


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def _req(ip="1.2.3.4"):
    return SimpleNamespace(headers={}, client=SimpleNamespace(host=ip))


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    # The per-IP limiter is module-level in-memory; reset it so tests don't bleed.
    pd._RL_HITS.clear()
    yield
    pd._RL_HITS.clear()


def test_non_eko_email_rejected(db):
    for bad in ("someone@gmail.com", "x@eko.co.in.evil.com", "y@notэko.co.in", "z@eko.com"):
        with pytest.raises(HTTPException) as e:
            request_code(_req(), EmailIn(email=bad), db=db)
        assert e.value.status_code == 403


def test_request_and_verify_flow(db, monkeypatch):
    captured = {}
    monkeypatch.setattr(pd, "_send_code_email", lambda to, code: captured.update(to=to, code=code) or True)

    r = request_code(_req(), EmailIn(email="Ceo@Eko.Co.In"), db=db)
    assert r["sent"] is True
    assert captured["to"] == "ceo@eko.co.in"                 # normalised
    code = captured["code"]
    assert len(code) == 6 and code.isdigit()

    # a wrong code is rejected but does not burn the code
    wrong = "000000" if code != "000000" else "111111"
    with pytest.raises(HTTPException) as e:
        verify_code(VerifyIn(email="ceo@eko.co.in", code=wrong), request=_req(), db=db)
    assert e.value.status_code == 400

    # the correct code mints a viewer session
    out = verify_code(VerifyIn(email="ceo@eko.co.in", code=code), request=_req(), db=db)
    assert out["user"]["role"] == "viewer"
    assert out["user"]["username"] == "ceo@eko.co.in"
    assert out["access_token"]
    # a single shared viewer principal backs all OTP sessions
    assert db.query(User).filter(User.username == "dashboard-viewer").count() == 1


def test_used_code_cannot_be_reused(db, monkeypatch):
    captured = {}
    monkeypatch.setattr(pd, "_send_code_email", lambda to, code: captured.update(code=code) or True)
    request_code(_req(), EmailIn(email="c@eko.co.in"), db=db)
    verify_code(VerifyIn(email="c@eko.co.in", code=captured["code"]), request=_req(), db=db)
    with pytest.raises(HTTPException) as e:
        verify_code(VerifyIn(email="c@eko.co.in", code=captured["code"]), request=_req(), db=db)
    assert e.value.status_code == 400            # already used → no active code


def test_expired_code_rejected(db, monkeypatch):
    captured = {}
    monkeypatch.setattr(pd, "_send_code_email", lambda to, code: captured.update(code=code) or True)
    request_code(_req(), EmailIn(email="b@eko.co.in"), db=db)
    otp = db.query(DashboardOtp).filter(DashboardOtp.email == "b@eko.co.in").first()
    otp.expires_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
    db.commit()
    with pytest.raises(HTTPException) as e:
        verify_code(VerifyIn(email="b@eko.co.in", code=captured["code"]), request=_req(), db=db)
    assert e.value.status_code == 400


def test_resend_cooldown(db, monkeypatch):
    monkeypatch.setattr(pd, "_send_code_email", lambda to, code: True)
    request_code(_req(), EmailIn(email="a@eko.co.in"), db=db)
    with pytest.raises(HTTPException) as e:
        request_code(_req(), EmailIn(email="a@eko.co.in"), db=db)
    assert e.value.status_code == 429


def test_per_ip_request_rate_limit(db, monkeypatch):
    # One IP fanning codes to DIFFERENT emails is capped (mail-bomb / relay abuse),
    # even though the per-email cooldown never triggers.
    monkeypatch.setattr(pd, "_send_code_email", lambda to, code: True)
    for i in range(pd.REQ_PER_MIN_IP):
        assert request_code(_req("9.9.9.9"), EmailIn(email=f"u{i}@eko.co.in"), db=db)["sent"] is True
    with pytest.raises(HTTPException) as e:
        request_code(_req("9.9.9.9"), EmailIn(email="u99@eko.co.in"), db=db)
    assert e.value.status_code == 429
    # a different IP is unaffected (limit is per-IP, not global)
    assert request_code(_req("8.8.8.8"), EmailIn(email="other@eko.co.in"), db=db)["sent"] is True


def test_per_ip_verify_rate_limit(db, monkeypatch):
    # One IP hammering verify is capped (brute-force control) regardless of email.
    monkeypatch.setattr(pd, "_send_code_email", lambda to, code: True)
    request_code(_req("7.7.7.7"), EmailIn(email="v@eko.co.in"), db=db)
    for _ in range(pd.VERIFY_PER_MIN_IP):
        with pytest.raises(HTTPException):
            verify_code(VerifyIn(email="v@eko.co.in", code="000000"), request=_req("7.7.7.7"), db=db)
    with pytest.raises(HTTPException) as e:
        verify_code(VerifyIn(email="v@eko.co.in", code="000000"), request=_req("7.7.7.7"), db=db)
    assert e.value.status_code == 429


def test_request_uniform_response_on_send_failure(db, monkeypatch):
    # A send failure must NOT change the response (no 500, no enumeration oracle).
    def boom(to, code):
        raise RuntimeError("smtp down")
    # _send_code_email swallows internally; simulate by making the inner send raise
    # but the wrapper return False → request_code still returns the uniform body.
    monkeypatch.setattr(pd, "_send_code_email", lambda to, code: False)
    r = request_code(_req("6.6.6.6"), EmailIn(email="x@eko.co.in"), db=db)
    assert r == {"sent": True}


def test_client_ip_cannot_be_spoofed_via_xff():
    # Rate-limit IP must come from nginx's X-Real-IP or the LAST (nginx-appended)
    # XFF hop — never the client-supplied leftmost value.
    NS = SimpleNamespace
    assert pd._client_ip(NS(headers={"x-real-ip": "9.9.9.9",
                                     "x-forwarded-for": "1.1.1.1, 9.9.9.9"},
                            client=NS(host="127.0.0.1"))) == "9.9.9.9"
    # no X-Real-IP → last hop, not the spoofed leftmost
    assert pd._client_ip(NS(headers={"x-forwarded-for": "6.6.6.6, 9.9.9.9"},
                            client=NS(host="127.0.0.1"))) == "9.9.9.9"
    # attacker rotating the leftmost does NOT change the resolved IP (same bucket)
    a = pd._client_ip(NS(headers={"x-forwarded-for": "rot1, 9.9.9.9"}, client=NS(host="127.0.0.1")))
    b = pd._client_ip(NS(headers={"x-forwarded-for": "rot2, 9.9.9.9"}, client=NS(host="127.0.0.1")))
    assert a == b == "9.9.9.9"


def test_oversized_local_part_rejected_cleanly(db):
    long_email = ("a" * 200) + "@eko.co.in"
    with pytest.raises(HTTPException) as e:
        request_code(_req(), EmailIn(email=long_email), db=db)
    assert e.value.status_code == 403     # clean reject, not a DB 500


def test_max_attempts_burns_code(db, monkeypatch):
    captured = {}
    monkeypatch.setattr(pd, "_send_code_email", lambda to, code: captured.update(code=code) or True)
    request_code(_req(), EmailIn(email="d@eko.co.in"), db=db)
    wrong = "000000" if captured["code"] != "000000" else "111111"
    for _ in range(pd.MAX_ATTEMPTS):
        with pytest.raises(HTTPException):
            verify_code(VerifyIn(email="d@eko.co.in", code=wrong), request=_req(), db=db)
    # even the CORRECT code no longer works once attempts are exhausted
    with pytest.raises(HTTPException) as e:
        verify_code(VerifyIn(email="d@eko.co.in", code=captured["code"]), request=_req(), db=db)
    assert e.value.status_code == 400
