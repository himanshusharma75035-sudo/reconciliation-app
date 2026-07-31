from fastapi import APIRouter, Depends, HTTPException, status, Request, Query, Header
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
import os
import json
import time
import hashlib
import secrets
import smtplib
import logging
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import datetime
from models.database import get_db, User, APIKey, generate_id, SessionLog, LoginOtp


def _log_session(db, event, username, user_id=None, request=None, detail=None):
    try:
        from core.auth import client_ip
        db.add(SessionLog(
            id=generate_id(), user_id=user_id, username=username, event=event,
            ip_address=client_ip(request) if request else None,
            user_agent=(request.headers.get("user-agent")[:300] if request and request.headers else None),
            detail=detail))
        db.commit()
    except Exception:
        db.rollback()
from core.auth import (verify_password, create_access_token, hash_password,
                       get_current_user, require_permission, require_admin,
                       generate_api_key, is_eko_email, login_2fa_enabled,
                       make_device_trust_token, device_trust_valid,
                       make_login_pending_token, login_pending_username)

logger = logging.getLogger("eko_recon.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])

# ── Login 2FA (email OTP) ─────────────────────────────────────────────────────
LOGIN_OTP_TTL_MIN      = 10
LOGIN_OTP_MAX_ATTEMPTS = 5
OTP_ISSUE_PER_USER     = 5     # OTP emails per username per 15 min (anti brute-force + anti mail-bomb)
OTP_ISSUE_WINDOW_S     = 900
VERIFY_PER_IP          = 20    # /verify-otp calls per IP per minute
VERIFY_PER_USER        = 15    # /verify-otp calls per username per 15 min

# In-memory sliding-window limiter (single-worker; same pattern as public_dashboard).
_RL_LOCK = threading.Lock()
_RL_HITS: dict = {}


def _client_ip(request) -> str:
    """Unspoofable client IP: nginx X-Real-IP, then the LAST XFF hop, then the peer."""
    h = getattr(request, "headers", None)
    if h:
        xri = h.get("x-real-ip")
        if xri:
            return xri.strip()
        xff = h.get("x-forwarded-for")
        if xff:
            return xff.split(",")[-1].strip()
    return getattr(getattr(request, "client", None), "host", "") or "unknown"


def _rate_ok(key: str, limit: int, window: int) -> bool:
    now = time.time()
    with _RL_LOCK:
        if len(_RL_HITS) > 5000:
            for k in [k for k, v in _RL_HITS.items() if not v or v[-1] < now - 3600]:
                _RL_HITS.pop(k, None)
            if len(_RL_HITS) > 20000:
                _RL_HITS.clear()
        hits = [t for t in _RL_HITS.get(key, ()) if t > now - window]
        if len(hits) >= limit:
            _RL_HITS[key] = hits
            return False
        hits.append(now)
        _RL_HITS[key] = hits
        return True


def _gen_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_code(code: str) -> str:
    return hashlib.sha256((code or "").encode()).hexdigest()


def _mask_email(email: str) -> str:
    try:
        local, dom = (email or "").split("@", 1)
        return (local[0] + "***@" + dom) if local else ("***@" + dom)
    except Exception:
        return "your email"


def _send_login_code(to: str, code: str) -> bool:
    """Email a login verification code. Returns True only if actually sent. NEVER
    raises (any config/SMTP error → False → the caller returns a clean 503)."""
    try:
        SMTP_HOST = os.getenv("SMTP_HOST", "")
        SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
        SMTP_USER = os.getenv("SMTP_USER", "")
        SMTP_PASS = os.getenv("SMTP_PASS", "")
        SMTP_FROM = os.getenv("SMTP_FROM", "recon@eko.co.in")
        if not SMTP_HOST:
            logger.warning("[auth] SMTP not configured — login code not emailed")
            return False
        msg = MIMEMultipart()
        msg["Subject"] = f"Your Eko login verification code: {code}"
        msg["From"] = SMTP_FROM
        msg["To"] = to
        html = f"""
        <div style="font-family:sans-serif;max-width:420px;margin:auto;padding:24px;color:#1f2937">
          <div style="background:#1e3a5f;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
            <div style="font-size:11px;opacity:.75">Eko Bharat Ventures — Reconciliation</div>
            <div style="font-size:16px;font-weight:700;margin-top:3px">Login verification</div>
          </div>
          <div style="border:1px solid #e5e7eb;border-top:none;padding:20px;border-radius:0 0 8px 8px;text-align:center">
            <div style="font-size:12px;color:#6b7280">Enter this code to finish signing in</div>
            <div style="font-size:34px;font-weight:800;letter-spacing:6px;margin:8px 0;color:#1e3a5f">{code}</div>
            <div style="font-size:12px;color:#6b7280">Expires in {LOGIN_OTP_TTL_MIN} minutes. If this wasn't you, change your password.</div>
          </div>
        </div>"""
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [to], msg.as_string())
        return True
    except Exception as e:
        logger.warning("[auth] login code email failed: %s", e)
        return False


def _issue_login_otp(db: Session, user: User) -> bool:
    """Create + email a fresh login OTP for user; burn earlier unused ones. Returns
    True if a code was actually sent."""
    email = (user.email or "").strip().lower()
    db.query(LoginOtp).filter(LoginOtp.username == user.username,
                              LoginOtp.used == False).update({"used": True})  # noqa: E712
    code = _gen_code()
    db.add(LoginOtp(id=generate_id(), username=user.username, email=email,
                    code_hash=_hash_code(code),
                    expires_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=LOGIN_OTP_TTL_MIN),
                    used=False, attempts=0))
    db.commit()
    return _send_login_code(email, code)


def _token_response(user: User, token: str) -> dict:
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "email": user.email,
            "role": user.role,
            "permissions": json.loads(user.permissions or "{}"),
            "allowed_products": json.loads(getattr(user, "allowed_products", None) or "[]"),
        },
    }

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    user: dict

class UserCreate(BaseModel):
    username: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    password: str
    role: str = "user"
    permissions: Optional[dict] = None
    allowed_products: Optional[list] = None   # [] / None = all products

class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    is_active: Optional[bool] = None
    permissions: Optional[dict] = None
    password: Optional[str] = None
    allowed_products: Optional[list] = None

class VerifyOtpIn(BaseModel):
    pending: str          # the login-pending token from /login (proves the password step)
    code: str
    remember_device: bool = True


@router.post("/login", response_model=None)
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(),
          x_device_trust: Optional[str] = Header(None, alias="X-Device-Trust"),
          db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        _log_session(db, "login_failed", form_data.username, request=request,
                     detail="bad credentials")
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        _log_session(db, "login_failed", user.username, user.id, request, "account disabled")
        raise HTTPException(status_code=403, detail="Account disabled")

    # ── Second factor: email OTP. Enforced for password accounts only (viewers are
    # dashboard-only, 2FA-exempt) when LOGIN_2FA is on and this browser isn't
    # already trusted. Setting LOGIN_2FA=off + restart is the break-glass. ────────
    if login_2fa_enabled() and user.role != "viewer":
        if not device_trust_valid(x_device_trust, user.username):
            if not is_eko_email(user.email):
                raise HTTPException(status_code=400,
                    detail="No @eko.co.in email on file for verification — ask an admin to set your email.")
            # Cap OTP emails per user so a stolen password can't grind the 6-digit
            # code by re-issuing (each new code would otherwise reset the per-code
            # attempt budget) and can't mail-bomb the user.
            if not _rate_ok(f"otp-issue:{user.username}", OTP_ISSUE_PER_USER, OTP_ISSUE_WINDOW_S):
                raise HTTPException(status_code=429,
                    detail="Too many verification codes requested — please wait a few minutes.")
            if not _issue_login_otp(db, user):
                raise HTTPException(status_code=503,
                    detail="Couldn't send your verification code right now — please try again in a moment.")
            _log_session(db, "login_otp_sent", user.username, user.id, request)
            # 'pending' proves the password step passed; /verify-otp requires it, so a
            # party who only knows the username can't touch the victim's OTP.
            return {"otp_required": True,
                    "pending": make_login_pending_token(user.username, minutes=LOGIN_OTP_TTL_MIN),
                    "email_hint": _mask_email(user.email)}

    token = create_access_token({"sub": user.username})
    _log_session(db, "login", user.username, user.id, request)
    return _token_response(user, token)


@router.post("/verify-otp", response_model=None)
def verify_login_otp(request: Request, body: VerifyOtpIn, db: Session = Depends(get_db)):
    """Second step of 2FA login: exchange the emailed code for a session (and,
    optionally, a 1-day trusted-device token so this browser skips the code next time).
    The username comes from the 'pending' token (proving the password step), NOT the
    request body — so a username alone can't spend a victim's OTP attempts."""
    username = login_pending_username(body.pending)
    if not username:
        raise HTTPException(status_code=401, detail="Session expired — sign in again.")
    ip = _client_ip(request)
    if (not _rate_ok(f"otp-verify-ip:{ip}", VERIFY_PER_IP, 60) or
            not _rate_ok(f"otp-verify-user:{username}", VERIFY_PER_USER, OTP_ISSUE_WINDOW_S)):
        raise HTTPException(status_code=429, detail="Too many attempts — please try again later.")
    user = db.query(User).filter(User.username == username).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Session expired — sign in again.")
    otp = (db.query(LoginOtp)
           .filter(LoginOtp.username == user.username, LoginOtp.used == False)  # noqa: E712
           .order_by(LoginOtp.created_at.desc()).first())
    if not otp:
        raise HTTPException(status_code=400, detail="No active code — sign in again to get a new one.")
    if otp.expires_at < datetime.datetime.utcnow():
        otp.used = True; db.commit()
        raise HTTPException(status_code=400, detail="Code expired — sign in again.")
    if (otp.attempts or 0) >= LOGIN_OTP_MAX_ATTEMPTS:
        otp.used = True; db.commit()
        raise HTTPException(status_code=400, detail="Too many attempts — sign in again.")
    if _hash_code((body.code or "").strip()) != otp.code_hash:
        otp.attempts = (otp.attempts or 0) + 1; db.commit()
        raise HTTPException(status_code=400, detail="Incorrect code.")

    otp.used = True; db.commit()
    token = create_access_token({"sub": user.username})
    _log_session(db, "login_2fa", user.username, user.id, request)
    resp = _token_response(user, token)
    if body.remember_device:
        resp["device_trust"] = make_device_trust_token(user.username, days=1)
    return resp

@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "full_name": current_user.full_name,
        "email": current_user.email,
        "role": current_user.role,
        "permissions": json.loads(current_user.permissions or "{}"),
        "allowed_products": json.loads(getattr(current_user, "allowed_products", None) or "[]"),
    }


@router.post("/logout")
def logout(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Record a logout event (JWT is stateless; the client discards the token)."""
    _log_session(db, "logout", current_user.username, current_user.id, request)
    return {"message": "Logged out"}


@router.get("/session-logs")
def session_logs(
    username: Optional[str] = None,
    event: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin-only: login / logout / failed-login / expiry history."""
    q = db.query(SessionLog)
    if username:  q = q.filter(SessionLog.username == username)
    if event:     q = q.filter(SessionLog.event == event)
    if from_date: q = q.filter(SessionLog.created_at >= f"{from_date} 00:00:00")
    if to_date:   q = q.filter(SessionLog.created_at <= f"{to_date} 23:59:59")
    total = q.count()
    rows = q.order_by(SessionLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "logs": [{
        "username": r.username, "event": r.event, "ip_address": r.ip_address,
        "user_agent": r.user_agent, "detail": r.detail, "created_at": str(r.created_at),
    } for r in rows]}

@router.get("/users")
def list_users(current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "full_name": u.full_name,
             "email": u.email, "role": u.role, "is_active": u.is_active,
             "permissions": json.loads(u.permissions or "{}"),
             "allowed_products": json.loads(getattr(u, "allowed_products", None) or "[]")} for u in users]

@router.post("/users")
def create_user(data: UserCreate, current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    # Email must be @eko.co.in. Password accounts (admin/user) REQUIRE one — it
    # receives their login verification code; viewers (dashboard-only) may omit it.
    email = (data.email or "").strip().lower() or None
    if data.role != "viewer":
        if not email:
            raise HTTPException(status_code=400,
                detail="An @eko.co.in email is required — it receives the login verification code.")
        if not is_eko_email(email):
            raise HTTPException(status_code=400, detail="Email must be an @eko.co.in address.")
    elif email and not is_eko_email(email):
        raise HTTPException(status_code=400, detail="Email must be an @eko.co.in address.")
    default_perms = {"upload": True, "run_recon": True, "src_assign": True, "reports": True, "logic_builder": False}
    perms = data.permissions or default_perms
    # Viewer accounts are dashboard-only: no operational permissions ever apply
    # (the server-side viewer scope in core/auth.py is the real boundary).
    if data.role == "viewer":
        perms = {}
    user = User(
        username=data.username,
        email=email,
        full_name=data.full_name,
        hashed_password=hash_password(data.password),
        role=data.role,
        permissions=json.dumps(perms),
        allowed_products=json.dumps(data.allowed_products or []),
    )
    db.add(user)
    db.commit()
    return {"id": user.id, "username": user.username, "message": "User created"}

@router.put("/users/{user_id}")
def update_user(user_id: str, data: UserUpdate, current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if data.full_name is not None: user.full_name = data.full_name
    if data.email is not None:
        email = (data.email or "").strip().lower() or None
        if email and not is_eko_email(email):
            raise HTTPException(status_code=400, detail="Email must be an @eko.co.in address.")
        if not email and user.role != "viewer":
            raise HTTPException(status_code=400,
                detail="An @eko.co.in email is required for this account (login verification).")
        user.email = email
    if data.is_active is not None: user.is_active = data.is_active
    if data.permissions is not None: user.permissions = json.dumps(data.permissions)
    if data.allowed_products is not None: user.allowed_products = json.dumps(data.allowed_products)
    if data.password: user.hashed_password = hash_password(data.password)
    db.commit()
    return {"message": "Updated successfully"}

@router.delete("/users/{user_id}")
def delete_user(user_id: str, current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete the root admin")
    db.delete(user)
    db.commit()
    return {"message": "Deleted"}



# ── API Key Management (admin only) ──────────────────────────────────────────

class APIKeyCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    permissions: Optional[dict] = None   # None = use default scoped permissions
    expires_days: Optional[int] = None   # None = never expires
    allowed_ips: Optional[str] = None    # CSV of IPs/CIDRs; None = any IP
    allowed_products: Optional[list] = None  # per-key product scope; [] / None = inherit the creator's

@router.post("/api-keys")
def create_api_key(
    body: APIKeyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Create a new API key (admin only). The plaintext key is returned ONCE — store it securely."""
    plaintext, key_hash, prefix = generate_api_key()
    default_perms = '{"upload":true,"run_recon":true,"src_assign":false,"reports":true,"logic_builder":false}'
    expires_at = None
    if body.expires_days:
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=body.expires_days)
    key = APIKey(
        id          = generate_id(),
        name        = body.name,
        description = body.description,
        key_hash    = key_hash,
        key_prefix  = prefix,
        permissions = json.dumps(body.permissions) if body.permissions else default_perms,
        is_active   = True,
        created_by  = current_user.id,
        expires_at  = expires_at,
        allowed_ips = (body.allowed_ips or "").strip() or None,
        allowed_products = json.dumps(body.allowed_products or []),
    )
    db.add(key)
    db.commit()
    return {
        "id":         key.id,
        "name":       key.name,
        "key_prefix": prefix,
        "api_key":    plaintext,   # ⚠ Only shown once — save this now
        "warning":    "This API key will NOT be shown again. Copy it now and store it securely.",
        "expires_at": str(expires_at) if expires_at else "never",
    }

@router.get("/api-keys")
def list_api_keys(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """List all API keys (admin only). Plaintext keys are never returned."""
    keys = db.query(APIKey).order_by(APIKey.created_at.desc()).all()
    return [{"id": k.id, "name": k.name, "description": k.description,
             "key_prefix": k.key_prefix, "is_active": k.is_active,
             "allowed_products": json.loads(getattr(k, "allowed_products", None) or "[]"),
             "last_used_at": str(k.last_used_at) if k.last_used_at else None,
             "expires_at": str(k.expires_at) if k.expires_at else "never",
             "created_at": str(k.created_at)} for k in keys]

@router.delete("/api-keys/{key_id}")
def revoke_api_key(
    key_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Revoke (deactivate) an API key immediately."""
    key = db.query(APIKey).filter(APIKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    key.is_active = False
    db.commit()
    return {"message": f"API key '{key.name}' revoked"}

@router.patch("/api-keys/{key_id}/restore")
def restore_api_key(
    key_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    key = db.query(APIKey).filter(APIKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    key.is_active = True
    db.commit()
    return {"message": f"API key '{key.name}' restored"}
