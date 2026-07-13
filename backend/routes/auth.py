from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
import json

import datetime
from models.database import get_db, User, APIKey, generate_id, SessionLog


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
                       generate_api_key)

router = APIRouter(prefix="/api/auth", tags=["auth"])

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

@router.post("/login", response_model=TokenResponse)
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        _log_session(db, "login_failed", form_data.username, request=request,
                     detail="bad credentials")
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        _log_session(db, "login_failed", user.username, user.id, request, "account disabled")
        raise HTTPException(status_code=403, detail="Account disabled")

    token = create_access_token({"sub": user.username})
    _log_session(db, "login", user.username, user.id, request)
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
        }
    }

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
    current_user: User = Depends(require_permission("admin")),
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
def list_users(current_user: User = Depends(require_permission("admin")), db: Session = Depends(get_db)):
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "full_name": u.full_name,
             "email": u.email, "role": u.role, "is_active": u.is_active,
             "permissions": json.loads(u.permissions or "{}"),
             "allowed_products": json.loads(getattr(u, "allowed_products", None) or "[]")} for u in users]

@router.post("/users")
def create_user(data: UserCreate, current_user: User = Depends(require_permission("admin")), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    default_perms = {"upload": True, "run_recon": True, "src_assign": True, "reports": True, "logic_builder": False}
    perms = data.permissions or default_perms
    # Viewer accounts are dashboard-only: no operational permissions ever apply
    # (the server-side viewer scope in core/auth.py is the real boundary).
    if data.role == "viewer":
        perms = {}
    user = User(
        username=data.username,
        email=data.email,
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
def update_user(user_id: str, data: UserUpdate, current_user: User = Depends(require_permission("admin")), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if data.full_name is not None: user.full_name = data.full_name
    if data.email is not None: user.email = data.email
    if data.is_active is not None: user.is_active = data.is_active
    if data.permissions is not None: user.permissions = json.dumps(data.permissions)
    if data.allowed_products is not None: user.allowed_products = json.dumps(data.allowed_products)
    if data.password: user.hashed_password = hash_password(data.password)
    db.commit()
    return {"message": "Updated successfully"}

@router.delete("/users/{user_id}")
def delete_user(user_id: str, current_user: User = Depends(require_permission("admin")), db: Session = Depends(get_db)):
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
