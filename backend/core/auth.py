import hashlib
import secrets
import json
import re
import logging
from datetime import datetime, timedelta
from typing import Optional

import ipaddress
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Header, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from models.database import get_db, User, APIKey
from core.config_audit import set_actor


def client_ip(request) -> str:
    """Best-effort client IP, honouring X-Forwarded-For (first hop) behind a proxy."""
    if request is None:
        return ""
    xff = request.headers.get("x-forwarded-for") if request.headers else None
    if xff:
        return xff.split(",")[0].strip()
    return getattr(getattr(request, "client", None), "host", "") or ""


def ip_allowed(ip: str, allowlist_csv: str) -> bool:
    """True if `ip` is in the comma-separated allowlist (plain IPs or CIDRs).
    An empty/None allowlist means 'allow from anywhere' (back-compat)."""
    if not allowlist_csv or not allowlist_csv.strip():
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowlist_csv.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False

logger = logging.getLogger("eko_recon.auth")

import os
# SECURITY: in production a SECRET_KEY env var is MANDATORY — startup fails without it.
# In dev we tolerate the fallback but log a prominent warning.
_ENV = os.getenv("ENV", os.getenv("APP_ENV", "dev")).lower()
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    if _ENV in ("production", "prod"):
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. "
            "Refusing to start in production with a default JWT secret."
        )
    SECRET_KEY = "eko-recon-secret-2026-change-in-production"
    logging.getLogger("eko_recon.auth").warning(
        "SECURITY WARNING: using built-in dev SECRET_KEY. "
        "Set the SECRET_KEY env var before deploying."
    )
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480  # 8 hours

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


# ── Dashboard-only "viewer" accounts ──────────────────────────────────────────
# A user whose role is "viewer" is restricted to the read-only executive analytics
# dashboard plus the minimal auth endpoints needed to sign in and bootstrap the
# SPA — and NOTHING else. Because virtually every protected endpoint depends on
# get_current_user, enforcing this allowlist centrally here locks these accounts
# down without per-route changes and with no risk that a forgotten read endpoint
# leaks transaction-level data. This is a hard server-side boundary; the frontend
# viewer redirect is only cosmetic on top of it.
VIEWER_ALLOWED_PREFIXES = (
    "/api/reports/analytics",   # the dashboard's only data source
    "/api/auth/me",             # SPA user bootstrap
    "/api/auth/logout",         # sign out
)


def _enforce_viewer_scope(user, request):
    """Raise 403 if a role='viewer' account touches anything outside the dashboard."""
    if getattr(user, "role", "") != "viewer":
        return
    path = getattr(getattr(request, "url", None), "path", "") if request is not None else ""
    # Fail closed: if we can't see the path, or it's not on the allowlist, deny.
    if not path or not any(path.startswith(p) for p in VIEWER_ALLOWED_PREFIXES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account can only view the executive dashboard.",
        )


# ── Password helpers ──────────────────────────────────────────────────────────

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


# ── JWT helpers ───────────────────────────────────────────────────────────────

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ── API Key helpers ───────────────────────────────────────────────────────────

def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.
    Returns (plaintext_key, key_hash, key_prefix).
    Plaintext must be shown to user ONCE — only the hash is stored.
    Format: eko_<32-hex-chars>
    """
    raw = secrets.token_hex(32)
    plaintext = f"eko_{raw}"
    key_hash  = hashlib.sha256(plaintext.encode()).hexdigest()
    prefix    = plaintext[:8]
    return plaintext, key_hash, prefix

def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


# ── Unified authentication ────────────────────────────────────────────────────
# Accepts EITHER:
#   Authorization: Bearer <jwt_token>
#   X-API-Key: eko_<32-hex>
#
# This allows both browser sessions (JWT) and programmatic agents (API key)
# to use the same endpoints.

def get_current_user(
    request: Request = None,
    token: Optional[str] = Depends(oauth2_scheme),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> User:
    """
    Resolve the current user from either a JWT token or an API key header.
    API key takes priority if both are provided.
    """
    # ── API Key path ──────────────────────────────────────────────────────────
    if x_api_key:
        key_hash = hash_api_key(x_api_key)
        api_key  = db.query(APIKey).filter(
            APIKey.key_hash == key_hash,
            APIKey.is_active == True,
        ).first()

        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "X-API-Key"},
            )
        if api_key.expires_at and api_key.expires_at < datetime.utcnow():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key has expired",
            )
        # IP allowlist (e.g. lock the OpenClaw agent key to its server IP)
        if not ip_allowed(client_ip(request), getattr(api_key, "allowed_ips", None)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key not permitted from this IP address",
            )

        # Update last_used_at (non-blocking — ignore errors)
        try:
            api_key.last_used_at = datetime.utcnow()
            db.commit()
        except Exception:
            db.rollback()

        # Synthesise a User-like object for the API key so downstream code
        # (permissions, audit logs) works without change.
        # We fetch the creator's User but override their permissions with the key's.
        creator = db.query(User).filter(User.id == api_key.created_by).first()
        if not creator or not creator.is_active:
            raise HTTPException(status_code=401, detail="API key owner account is inactive")

        # Override permissions with key-specific scope. CRITICAL: a key's authority is
        # its OWN scope, never the admin creator's role — otherwise every admin-minted
        # key would silently carry full admin authority and its declared scope would be
        # ignored. So mark it as an API-key principal and strip the inherited admin role;
        # a key gets admin authority ONLY by opting in via its permissions ('{"admin":true}').
        creator_copy = User.__new__(User)
        creator_copy.__dict__.update(creator.__dict__)
        creator_copy.permissions = api_key.permissions
        creator_copy.full_name   = f"{api_key.name} [API key]"
        creator_copy.is_api_key  = True
        creator_copy.role        = "user"
        # Product scope: a key uses its OWN allowed_products when it declares any; otherwise it
        # inherits the creator's — so an unscoped key is bounded by its creator, never broader,
        # and a scoped key can be narrower than its creator but never exceed the intended set.
        try:
            _kp = getattr(api_key, "allowed_products", None)
            if _kp and json.loads(_kp):
                creator_copy.allowed_products = _kp
        except Exception:
            pass
        set_actor(db, creator_copy)   # additive: attribute config/entitlement changes to this principal
        _enforce_viewer_scope(creator_copy, request)
        return creator_copy

    # ── JWT path ──────────────────────────────────────────────────────────────
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token — provide a Bearer token or X-API-Key header",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exception
    try:
        payload  = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise credentials_exception
        # Non-session token types (they also carry sub=<username> but must NEVER
        # authenticate a request): "device_trust" only lets a browser skip the 2FA
        # code on /login; "login_pending" only lets /verify-otp finish a 2FA login.
        if payload.get("typ") in ("device_trust", "login_pending"):
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.username == username).first()
    if user is None or not user.is_active:
        raise credentials_exception
    set_actor(db, user)   # additive: attribute config/entitlement changes to this principal
    _enforce_viewer_scope(user, request)
    return user


# ── Permission helpers ────────────────────────────────────────────────────────

def is_admin_principal(user) -> bool:
    """Does this principal hold admin authority?

    A normal (JWT-authenticated) user is admin by role. An API-key principal is
    admin ONLY when its own scope explicitly grants it (permissions '{"admin":true}')
    — never merely because an admin minted the key. This is the single place that
    decides admin authority, so every gate (require_permission / require_admin /
    check_product_access) treats a scoped key by its declared scope, not the
    creator's role."""
    if getattr(user, "is_api_key", False):
        try:
            return bool(json.loads(getattr(user, "permissions", None) or "{}").get("admin"))
        except Exception:
            return False
    return getattr(user, "role", "") == "admin"


def check_product_access(db, user, partner: str):
    """
    Raise 403 if the user's allowed_products excludes this partner's product.
    Empty/None allowed_products = access to ALL products. Admins always pass.
    """
    if is_admin_principal(user):
        return
    try:
        allowed = json.loads(getattr(user, "allowed_products", None) or "[]")
    except Exception:
        allowed = []
    if not allowed:
        return
    from fastapi import HTTPException as _HTTPException
    from models.database import PartnerConfig
    pc = db.query(PartnerConfig).filter(PartnerConfig.slug == partner).first()
    product = (pc.product if pc else partner) or partner
    if product not in allowed:
        raise _HTTPException(
            status_code=403,
            detail=f"Access denied: your account is not assigned to the '{product}' product.",
        )


def require_product_access(partner: str):
    """Router-level dependency: 403 if the user's allowed_products excludes this
    product's partner. Admins and users with empty allowed_products pass (see
    check_product_access). Attach to a module router as
    `APIRouter(..., dependencies=[Depends(require_product_access("evalue"))])` so
    every endpoint on it — reads included — is gated exactly like core recon is."""
    def _dep(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        check_product_access(db, user, partner)
        return user
    return _dep


def require_permission(permission: str):
    def checker(current_user: User = Depends(get_current_user)):
        if is_admin_principal(current_user):
            return current_user
        try:
            perms = json.loads(current_user.permissions or "{}")
        except Exception:
            perms = {}
        if not perms.get(permission, False):
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: '{permission}' required",
            )
        return current_user
    return checker


def require_admin(current_user: User = Depends(get_current_user)):
    if not is_admin_principal(current_user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# ── Login 2FA (email OTP) helpers ─────────────────────────────────────────────
# All app-user and dashboard emails must be @eko.co.in.
EKO_EMAIL_RE = re.compile(r"^[^@\s]+@eko\.co\.in$", re.IGNORECASE)


def is_eko_email(email: str) -> bool:
    return bool(EKO_EMAIL_RE.match((email or "").strip().lower()))


def login_2fa_enabled() -> bool:
    """Master switch for login two-factor (email OTP). OFF unless LOGIN_2FA is
    truthy in the environment — so a bad rollout or SMTP outage is cleared by
    setting LOGIN_2FA=off and restarting the backend (the break-glass)."""
    return os.getenv("LOGIN_2FA", "off").strip().lower() in ("1", "on", "true", "yes")


def make_device_trust_token(username: str, days: int = 1) -> str:
    """A short-lived token that lets THIS browser skip the 2FA code on the next
    login. Carries typ='device_trust' so get_current_user refuses it as a session."""
    return create_access_token({"sub": username, "typ": "device_trust"},
                               expires_delta=timedelta(days=days))


def device_trust_valid(token: str, username: str) -> bool:
    """True if `token` is a still-valid device-trust token for `username`."""
    if not token:
        return False
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return False
    return payload.get("typ") == "device_trust" and payload.get("sub") == username


def make_login_pending_token(username: str, minutes: int = 10) -> str:
    """Short-lived token proving the password step passed. /verify-otp requires it,
    so a party who knows only a username can't touch a victim's in-flight OTP."""
    return create_access_token({"sub": username, "typ": "login_pending"},
                               expires_delta=timedelta(minutes=minutes))


def login_pending_username(token: str) -> Optional[str]:
    """Return the username from a valid login_pending token, else None."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    return payload.get("sub") if payload.get("typ") == "login_pending" else None


# ── Seed admin ────────────────────────────────────────────────────────────────

def seed_admin(db: Session):
    existing = db.query(User).filter(User.username == "admin").first()
    if not existing:
        admin = User(
            username="admin",
            email="admin@eko.co.in",
            hashed_password=hash_password("Admin@1234"),
            role="admin",
            full_name="Admin",
            permissions='{"upload":true,"run_recon":true,"src_assign":true,"reports":true,"logic_builder":true}',
        )
        db.add(admin)
        db.commit()
        print("✅ Default admin created → username: admin | password: Admin@1234")
