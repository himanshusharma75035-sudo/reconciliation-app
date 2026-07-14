"""
routes/public_dashboard.py

Passwordless, domain-restricted access to the executive dashboard (/exec).

Anyone with an @eko.co.in email can view the dashboard: they request a 6-digit
code, it is emailed to that address, and they exchange it for a SHORT-LIVED
VIEWER session (role=viewer → server-side scope in core/auth.py locks it to the
analytics endpoint only). No password, no admin provisioning. Access is gated by
control of an @eko.co.in mailbox.

These two endpoints are intentionally UNAUTHENTICATED (they are the pre-auth
gate); everything they hand out is dashboard-only. The code is stored only as a
SHA-256 hash, is single-use, time-boxed, attempt-capped, and rate-limited.
"""
import os
import re
import time
import hashlib
import secrets
import smtplib
import datetime
import logging
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db, DashboardOtp, User, generate_id
from core.auth import create_access_token, hash_password

logger = logging.getLogger("eko_recon.public_dashboard")
router = APIRouter(prefix="/api/public/dashboard", tags=["public-dashboard"])

EKO_RE = re.compile(r"^[^@\s]+@eko\.co\.in$", re.IGNORECASE)
CODE_TTL_MIN       = 10     # a code is valid for 10 minutes
SESSION_HOURS      = 12     # the viewer session lasts 12 hours, then re-verify
RESEND_COOLDOWN_S  = 60     # min seconds between code requests for one email
MAX_ATTEMPTS       = 5      # wrong-code guesses before a code is burned
MAX_EMAIL_LEN      = 190    # guard the VARCHAR(200) email column (avoid a DB 500)
VIEWER_USERNAME    = "dashboard-viewer"   # shared viewer principal for OTP sessions

# Per-IP throttles on these UNAUTHENTICATED endpoints — the real anti-automation
# control (the per-email cooldown is bypassable by rotating the local part). These
# bound mail-bombing (request) and brute-force (verify). We key off an UNSPOOFABLE
# client IP (see _client_ip) — NOT the leftmost X-Forwarded-For, which is
# client-supplied and would let an attacker rotate buckets to bypass the limits.
REQ_PER_MIN_IP     = 5
REQ_PER_HOUR_IP    = 30
VERIFY_PER_MIN_IP  = 12
VERIFY_PER_HOUR_IP = 60

# In-memory sliding-window limiter. Safe because the app runs as a SINGLE worker
# (same assumption as the match-ID lock); slowapi's default storage is likewise
# in-memory. Keyed by "<bucket>:<ip>".
_RL_LOCK = threading.Lock()
_RL_HITS: dict[str, list] = {}


def _client_ip(request) -> str:
    """UNSPOOFABLE client IP for rate-limiting. Prefer nginx's X-Real-IP (set from
    $remote_addr = the real TCP peer nginx observed — a client cannot forge it),
    then the LAST X-Forwarded-For hop (the one nginx APPENDED via
    $proxy_add_x_forwarded_for — earlier hops are attacker-supplied), then the socket
    peer. Never trust the leftmost XFF."""
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
    """True if this key is under `limit` hits in the last `window` seconds (records the hit)."""
    now = time.time()
    with _RL_LOCK:
        if len(_RL_HITS) > 5000:   # prune stale keys so the dict can't grow unbounded
            for k in [k for k, v in _RL_HITS.items() if not v or v[-1] < now - 3600]:
                _RL_HITS.pop(k, None)
            if len(_RL_HITS) > 20000:   # hard backstop against OOM under a flood
                _RL_HITS.clear()
        hits = [t for t in _RL_HITS.get(key, ()) if t > now - window]
        if len(hits) >= limit:
            _RL_HITS[key] = hits
            return False
        hits.append(now)
        _RL_HITS[key] = hits
        return True


class EmailIn(BaseModel):
    email: str


class VerifyIn(BaseModel):
    email: str
    code: str


def _now():
    return datetime.datetime.utcnow()


def _norm_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if len(email) > MAX_EMAIL_LEN or not EKO_RE.match(email):
        raise HTTPException(status_code=403,
                            detail="Only @eko.co.in email addresses can access the dashboard.")
    return email


def _send_code_email(to: str, code: str) -> bool:
    """Email the 6-digit code. Returns True if actually sent (SMTP configured)."""
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASS = os.getenv("SMTP_PASS", "")
    SMTP_FROM = os.getenv("SMTP_FROM", "recon@eko.co.in")
    if not SMTP_HOST:
        logger.warning("[public_dashboard] SMTP not configured — code not emailed")
        return False
    try:
        msg = MIMEMultipart()
        msg["Subject"] = f"Your Eko dashboard access code: {code}"
        msg["From"] = SMTP_FROM
        msg["To"] = to
        html = f"""
        <div style="font-family:sans-serif;max-width:420px;margin:auto;padding:24px;color:#1f2937">
          <div style="background:#065f46;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
            <div style="font-size:11px;opacity:.75">Eko Bharat Ventures</div>
            <div style="font-size:16px;font-weight:700;margin-top:3px">Executive Dashboard access</div>
          </div>
          <div style="border:1px solid #e5e7eb;border-top:none;padding:20px;border-radius:0 0 8px 8px;text-align:center">
            <div style="font-size:12px;color:#6b7280">Your one-time code</div>
            <div style="font-size:34px;font-weight:800;letter-spacing:6px;margin:8px 0;color:#065f46">{code}</div>
            <div style="font-size:12px;color:#6b7280">Expires in {CODE_TTL_MIN} minutes. If you didn't request this, ignore this email.</div>
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
        # NEVER let a send failure raise: it would 500 the endpoint AND (if the
        # relay rejects unknown recipients) leak mailbox existence via the status.
        logger.warning("[public_dashboard] code email failed: %s", e)
        return False


@router.post("/request-code")
def request_code(request: Request, body: EmailIn, db: Session = Depends(get_db)):
    """Email a fresh 6-digit code to an @eko.co.in address (rate-limited per IP + per email)."""
    email = _norm_email(body.email)
    ip = _client_ip(request)
    if not _rate_ok(f"rc1m:{ip}", REQ_PER_MIN_IP, 60) or not _rate_ok(f"rc1h:{ip}", REQ_PER_HOUR_IP, 3600):
        raise HTTPException(status_code=429, detail="Too many requests — please try again in a minute.")

    recent = (db.query(DashboardOtp)
              .filter(DashboardOtp.email == email)
              .order_by(DashboardOtp.created_at.desc()).first())
    if recent and (_now() - recent.created_at).total_seconds() < RESEND_COOLDOWN_S:
        raise HTTPException(status_code=429,
                            detail="Please wait a minute before requesting another code.")

    # Burn any still-valid earlier codes for this email (only the newest works).
    db.query(DashboardOtp).filter(DashboardOtp.email == email,
                                  DashboardOtp.used == False).update({"used": True})  # noqa: E712

    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(DashboardOtp(
        id=generate_id(), email=email,
        code_hash=hashlib.sha256(code.encode()).hexdigest(),
        expires_at=_now() + datetime.timedelta(minutes=CODE_TTL_MIN),
        used=False, attempts=0,
    ))
    db.commit()

    # Fire-and-forget: the return value is intentionally ignored so the response is
    # IDENTICAL (status + body) whether or not the mailbox exists or the send fails —
    # no enumeration oracle, no unhandled 500.
    _send_code_email(email, code)
    return {"sent": True}


@router.post("/verify-code")
def verify_code(body: VerifyIn, request: Request, db: Session = Depends(get_db)):
    """Exchange a valid code for a short-lived dashboard-only (viewer) session."""
    email = _norm_email(body.email)
    code = (body.code or "").strip()
    ip = _client_ip(request)
    # Per-IP verify throttle — the primary brute-force control (a determined attacker
    # can create codes for guessed addresses; this caps how fast they can guess).
    if not _rate_ok(f"vf1m:{ip}", VERIFY_PER_MIN_IP, 60) or not _rate_ok(f"vf1h:{ip}", VERIFY_PER_HOUR_IP, 3600):
        raise HTTPException(status_code=429, detail="Too many attempts — please try again later.")

    otp = (db.query(DashboardOtp)
           .filter(DashboardOtp.email == email, DashboardOtp.used == False)  # noqa: E712
           .order_by(DashboardOtp.created_at.desc()).first())
    if not otp:
        raise HTTPException(status_code=400, detail="No active code — request a new one.")
    if otp.expires_at < _now():
        otp.used = True; db.commit()
        raise HTTPException(status_code=400, detail="Code expired — request a new one.")
    if (otp.attempts or 0) >= MAX_ATTEMPTS:
        otp.used = True; db.commit()
        raise HTTPException(status_code=400, detail="Too many attempts — request a new code.")
    if hashlib.sha256(code.encode()).hexdigest() != otp.code_hash:
        otp.attempts = (otp.attempts or 0) + 1
        db.commit()
        raise HTTPException(status_code=400, detail="Incorrect code.")

    otp.used = True
    db.commit()

    # Shared, dashboard-only VIEWER principal (created once). Individual access is
    # audited by email in SessionLog below; the token subject is this principal so
    # get_current_user + the viewer scope apply unchanged.
    viewer = db.query(User).filter(User.username == VIEWER_USERNAME).first()
    if not viewer:
        viewer = User(
            username=VIEWER_USERNAME, role="viewer", is_active=True,
            full_name="Dashboard Viewer (email code)",
            hashed_password=hash_password(secrets.token_hex(24)),  # unusable — no password login
            permissions="{}", allowed_products="[]",
        )
        db.add(viewer); db.commit()
    if not viewer.is_active:
        raise HTTPException(status_code=403, detail="Dashboard access is currently disabled.")

    token = create_access_token({"sub": VIEWER_USERNAME, "em": email},
                                expires_delta=datetime.timedelta(hours=SESSION_HOURS))

    try:
        from routes.auth import _log_session
        _log_session(db, "dashboard_login", email, viewer.id, request, "email code")
    except Exception:
        pass

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"username": email, "full_name": email, "role": "viewer", "permissions": {}},
    }
