import logging
logger = logging.getLogger("eko_recon")

"""
routes/admin.py

Admin configuration endpoints — Partners, Format Presets, Matching Rules.
All write endpoints require admin role.
GET /api/partners is public (used by Upload, RunRecon, Dashboard dropdowns).
"""

import os
import json
import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import (
    get_db, PartnerConfig, BankFormatPreset, MatchRule, generate_id,
    SystemSetting, AuditLog,
)
from core.auth import require_permission, require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── Public partner list (used by Upload, RunRecon) ────────────────────────────

@router.get("/partners-public")
def get_active_partners(
    product: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Public endpoint — no auth required.
    Returns active partners, optionally filtered by product (dmt | aeps | pg).
    Used by Upload page and RunRecon selectors.
    """
    q = db.query(PartnerConfig).filter(PartnerConfig.is_active == True)
    if product:
        q = q.filter(PartnerConfig.product == product)
    partners = q.order_by(PartnerConfig.sort_order).all()
    return [_partner_out(p) for p in partners]


# ── Partner CRUD ──────────────────────────────────────────────────────────────

class PartnerIn(BaseModel):
    slug:               str
    display_name:       str
    match_prefix:       str
    product:            str = "dmt"
    source_value:       Optional[str] = None
    has_bank_statement: bool = True
    has_internal_dump:  bool = True
    is_active:          bool = True
    sort_order:         int  = 0
    notes:              Optional[str] = None
    settlement_carry_days: int = 1


def _partner_out(p: PartnerConfig) -> dict:
    return {
        "id":                 p.id,
        "slug":               p.slug,
        "display_name":       p.display_name,
        "match_prefix":       p.match_prefix,
        "product":            p.product,
        "source_value":       p.source_value,
        "has_bank_statement": p.has_bank_statement,
        "has_internal_dump":  p.has_internal_dump,
        "is_active":          p.is_active,
        "sort_order":         p.sort_order,
        "notes":              p.notes,
        "settlement_carry_days": getattr(p, "settlement_carry_days", 1),
        "created_at":         p.created_at.isoformat() if p.created_at else None,
        "updated_at":         p.updated_at.isoformat() if p.updated_at else None,
    }


@router.get("/partners")
def list_partners(db: Session = Depends(get_db), _=Depends(require_admin)):
    partners = db.query(PartnerConfig).order_by(PartnerConfig.sort_order).all()
    return [_partner_out(p) for p in partners]


@router.post("/partners")
def create_partner(body: PartnerIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    body.slug = body.slug.strip().lower()
    if db.query(PartnerConfig).filter(PartnerConfig.slug == body.slug).first():
        raise HTTPException(status_code=400, detail=f"Partner '{body.slug}' already exists")
    if len(body.match_prefix) > 5:
        raise HTTPException(status_code=400, detail="Match prefix must be ≤ 5 characters")
    p = PartnerConfig(**body.dict())
    db.add(p)
    db.commit()
    db.refresh(p)
    return _partner_out(p)


@router.put("/partners/{partner_id}")
def update_partner(
    partner_id: str, body: PartnerIn,
    db: Session = Depends(get_db), _=Depends(require_admin)
):
    p = db.query(PartnerConfig).filter(PartnerConfig.id == partner_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Partner not found")
    # Slug changes are allowed but warn if it breaks existing transactions
    for field, val in body.dict().items():
        setattr(p, field, val)
    p.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(p)
    return _partner_out(p)


@router.delete("/partners/{partner_id}")
def deactivate_partner(
    partner_id: str, db: Session = Depends(get_db), _=Depends(require_admin)
):
    p = db.query(PartnerConfig).filter(PartnerConfig.id == partner_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Partner not found")
    p.is_active = False
    p.updated_at = datetime.datetime.utcnow()
    db.commit()
    return {"status": "deactivated", "slug": p.slug}


# ── Format Preset CRUD ────────────────────────────────────────────────────────

class FormatPresetIn(BaseModel):
    partner_slug:   str
    side:           str                  # bank | internal
    label:          str
    signature_cols: str                  # JSON string: ["col1", "col2"]
    column_mapping: str                  # JSON string: {"std_field": "file_col"}
    special_config: Optional[str] = "{}" # JSON string


def _preset_out(p: BankFormatPreset) -> dict:
    return {
        "id":             p.id,
        "partner_slug":   p.partner_slug,
        "side":           p.side,
        "label":          p.label,
        "signature_cols": json.loads(p.signature_cols or "[]"),
        "column_mapping": json.loads(p.column_mapping or "{}"),
        "special_config": json.loads(p.special_config or "{}"),
        "is_active":      p.is_active,
        "created_at":     p.created_at.isoformat() if p.created_at else None,
        "updated_at":     p.updated_at.isoformat() if p.updated_at else None,
    }


@router.get("/format-presets")
def list_format_presets(
    partner_slug: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(require_admin)
):
    q = db.query(BankFormatPreset)
    if partner_slug:
        q = q.filter(BankFormatPreset.partner_slug == partner_slug)
    presets = q.order_by(BankFormatPreset.partner_slug, BankFormatPreset.side).all()
    return [_preset_out(p) for p in presets]


@router.post("/format-presets")
def create_format_preset(
    body: FormatPresetIn, db: Session = Depends(get_db), _=Depends(require_admin)
):
    # Validate JSON fields
    for field_name, val in [("signature_cols", body.signature_cols), ("column_mapping", body.column_mapping)]:
        try:
            json.loads(val)
        except Exception:
            raise HTTPException(status_code=400, detail=f"Invalid JSON in {field_name}")

    preset = BankFormatPreset(
        partner_slug=body.partner_slug.lower(),
        side=body.side,
        label=body.label,
        signature_cols=body.signature_cols,
        column_mapping=body.column_mapping,
        special_config=body.special_config or "{}",
    )
    db.add(preset)
    db.commit()
    db.refresh(preset)
    return _preset_out(preset)


@router.put("/format-presets/{preset_id}")
def update_format_preset(
    preset_id: str, body: FormatPresetIn,
    db: Session = Depends(get_db), _=Depends(require_admin)
):
    preset = db.query(BankFormatPreset).filter(BankFormatPreset.id == preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Format preset not found")
    for field_name, val in [("signature_cols", body.signature_cols), ("column_mapping", body.column_mapping)]:
        try:
            json.loads(val)
        except Exception:
            raise HTTPException(status_code=400, detail=f"Invalid JSON in {field_name}")
    preset.partner_slug  = body.partner_slug.lower()
    preset.side          = body.side
    preset.label         = body.label
    preset.signature_cols = body.signature_cols
    preset.column_mapping = body.column_mapping
    preset.special_config = body.special_config or "{}"
    preset.updated_at    = datetime.datetime.utcnow()
    db.commit()
    db.refresh(preset)
    return _preset_out(preset)


@router.delete("/format-presets/{preset_id}")
def deactivate_format_preset(
    preset_id: str, db: Session = Depends(get_db), _=Depends(require_admin)
):
    preset = db.query(BankFormatPreset).filter(BankFormatPreset.id == preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Format preset not found")
    preset.is_active = False
    preset.updated_at = datetime.datetime.utcnow()
    db.commit()
    return {"status": "deactivated"}


# ── Matching Rules CRUD (per partner) ─────────────────────────────────────────

class MatchRuleIn(BaseModel):
    name:         str
    partner:      str
    priority:     int
    match_fields: str   # JSON list: ["eko_tid", "tracking_number"]
    description:  Optional[str] = None
    scope:        str = "bank_internal"   # bank_internal | bank_bank | internal_internal


def _rule_out(r: MatchRule) -> dict:
    return {
        "id":           r.id,
        "name":         r.name,
        "partner":      r.partner,
        "priority":     r.priority,
        "match_fields": json.loads(r.match_fields or "[]"),
        "is_active":    r.is_active,
        "description":  r.description,
        "scope":        r.scope or "bank_internal",
        "created_at":   r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/match-rules")
def list_match_rules(
    partner: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(require_admin)
):
    q = db.query(MatchRule)
    if partner:
        q = q.filter(MatchRule.partner == partner)
    rules = q.order_by(MatchRule.partner, MatchRule.priority).all()
    return [_rule_out(r) for r in rules]


@router.post("/match-rules")
def create_match_rule(
    body: MatchRuleIn, db: Session = Depends(get_db), _=Depends(require_admin)
):
    try:
        fields = json.loads(body.match_fields)
        if not isinstance(fields, list) or len(fields) == 0:
            raise ValueError()
    except Exception:
        raise HTTPException(status_code=400, detail="match_fields must be a non-empty JSON array")
    if (body.scope or "bank_internal") not in ("bank_internal", "bank_bank", "internal_internal"):
        raise HTTPException(status_code=400, detail="scope must be bank_internal | bank_bank | internal_internal")

    rule = MatchRule(
        name=body.name,
        partner=body.partner.lower(),
        priority=body.priority,
        match_fields=json.dumps(fields),
        is_active=True,
        description=body.description,
        scope=(body.scope or "bank_internal"),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return _rule_out(rule)


@router.put("/match-rules/{rule_id}")
def update_match_rule(
    rule_id: str, body: MatchRuleIn,
    db: Session = Depends(get_db), _=Depends(require_admin)
):
    rule = db.query(MatchRule).filter(MatchRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    try:
        fields = json.loads(body.match_fields)
        if not isinstance(fields, list) or len(fields) == 0:
            raise ValueError()
    except Exception:
        raise HTTPException(status_code=400, detail="match_fields must be a non-empty JSON array")
    if (body.scope or "bank_internal") not in ("bank_internal", "bank_bank", "internal_internal"):
        raise HTTPException(status_code=400, detail="scope must be bank_internal | bank_bank | internal_internal")
    rule.name         = body.name
    rule.partner      = body.partner.lower()
    rule.priority     = body.priority
    rule.match_fields = json.dumps(fields)
    rule.description  = body.description
    rule.scope        = (body.scope or "bank_internal")
    db.commit()
    db.refresh(rule)
    return _rule_out(rule)


@router.patch("/match-rules/{rule_id}/toggle")
def toggle_match_rule(
    rule_id: str, db: Session = Depends(get_db), _=Depends(require_admin)
):
    rule = db.query(MatchRule).filter(MatchRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    rule.is_active = not rule.is_active
    db.commit()
    return _rule_out(rule)


@router.delete("/match-rules/{rule_id}")
def delete_match_rule(
    rule_id: str, db: Session = Depends(get_db), _=Depends(require_admin)
):
    rule = db.query(MatchRule).filter(MatchRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.delete(rule)
    db.commit()
    return {"status": "deleted"}


# ── Bank Accounts (per-partner) ───────────────────────────────────────────────
# A DMT partner (e.g. Axis) can hold several current accounts. Statements are
# tagged with their account number at ingest; recon stays partner-level. Admins
# register/disable accounts here; uploading a new account auto-discovers it too.

from models.database import BankAccount


def _bank_account_out(a: BankAccount) -> dict:
    return {
        "id": a.id, "partner": a.partner, "account_number": a.account_number,
        "label": a.label, "ifsc": a.ifsc, "is_primary": bool(a.is_primary),
        "is_active": bool(a.is_active), "auto_added": bool(a.auto_added),
    }


class BankAccountIn(BaseModel):
    partner:        str
    account_number: str
    label:          Optional[str] = None
    ifsc:           Optional[str] = None
    is_active:      bool = True
    is_primary:     bool = False


@router.get("/bank-accounts")
def list_bank_accounts(partner: Optional[str] = None, db: Session = Depends(get_db),
                       _=Depends(require_permission("upload"))):
    """List registered bank accounts, optionally filtered by partner."""
    q = db.query(BankAccount)
    if partner:
        q = q.filter(BankAccount.partner == partner)
    rows = q.order_by(BankAccount.partner, BankAccount.is_primary.desc(),
                      BankAccount.account_number).all()
    return [_bank_account_out(a) for a in rows]


@router.post("/bank-accounts")
def add_bank_account(body: BankAccountIn, db: Session = Depends(get_db),
                     _=Depends(require_admin)):
    acct = (body.account_number or "").strip()
    if not acct or not body.partner:
        raise HTTPException(status_code=400, detail="partner and account_number are required")
    if db.query(BankAccount).filter(BankAccount.account_number == acct).first():
        raise HTTPException(status_code=409, detail=f"Account {acct} already registered")
    row = BankAccount(
        partner=body.partner.strip().lower(), account_number=acct,
        label=body.label or f"{body.partner.title()} — A/c …{acct[-4:]}",
        ifsc=body.ifsc, is_active=body.is_active, is_primary=body.is_primary,
        auto_added=False,
    )
    db.add(row); db.commit(); db.refresh(row)
    return _bank_account_out(row)


@router.put("/bank-accounts/{account_id}")
def update_bank_account(account_id: str, body: BankAccountIn, db: Session = Depends(get_db),
                        _=Depends(require_admin)):
    row = db.query(BankAccount).filter(BankAccount.id == account_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Bank account not found")
    row.partner        = body.partner.strip().lower()
    row.account_number = body.account_number.strip()
    row.label          = body.label
    row.ifsc           = body.ifsc
    row.is_active      = body.is_active
    row.is_primary     = body.is_primary
    db.commit(); db.refresh(row)
    return _bank_account_out(row)


@router.delete("/bank-accounts/{account_id}")
def delete_bank_account(account_id: str, db: Session = Depends(get_db),
                        _=Depends(require_admin)):
    row = db.query(BankAccount).filter(BankAccount.id == account_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Bank account not found")
    db.delete(row); db.commit()
    return {"status": "deleted"}


# ── AI configuration (Anthropic API key) ──────────────────────────────────────
# The key that powers the Developer-Portal agent and (going forward) AI reconciliation.
# Stored in SystemSetting so admins set it here instead of editing backend/.env. The value
# is never returned — only a masked preview + configured/source status. config_audit blocks
# 'api_key' from the change log, and the audit row we write carries no key material.

class AIConfigIn(BaseModel):
    # Anthropic key powers the Developer-Portal / Builder agent AND is the fallback for recon-AI
    # when its provider is 'anthropic'. The recon-AI provider fields below are independent, so
    # reconciliation intelligence can run on Anthropic, OpenAI, or any OpenAI-compatible /
    # open-source endpoint (Ollama, vLLM, OpenRouter, Groq, Together, Gemini-openai, …).
    # Any field left null is unchanged; "" clears it.
    api_key:    Optional[str] = None       # -> anthropic_api_key
    provider:   Optional[str] = None       # -> ai_provider : 'anthropic' | 'openai'
    ai_api_key: Optional[str] = None       # -> ai_api_key  (blank ⇒ use the Anthropic key when provider='anthropic')
    base_url:   Optional[str] = None       # -> ai_base_url (blank ⇒ OpenAI default; set for self-hosted / other)
    model:      Optional[str] = None       # -> ai_model


def _mask_key(k: str) -> str:
    k = (k or "").strip()
    if not k:
        return ""
    return (k[:7] + "…" + k[-4:]) if len(k) > 14 else "set"


def _ai_status(db) -> dict:
    """The Configuration → AI view: recon-AI provider state + the (separate) Anthropic key that
    powers the Builder agent. Key material is never returned — only masked previews."""
    from core import ai_client

    def _val(k):
        r = db.query(SystemSetting).filter(SystemSetting.key == k).first()
        return ((r.value if r else "") or "").strip()
    cfg = ai_client.ai_config()
    anth = _val("anthropic_api_key") or (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    return {
        # reconciliation AI (multi-provider)
        "provider":     cfg["provider"],
        "base_url":     _val("ai_base_url"),
        "model":        cfg["model"],
        "ai_configured": ai_client.is_enabled(),
        "ai_masked":    _mask_key(_val("ai_api_key")),
        # Developer-Portal / Builder agent (Anthropic only)
        "configured":   bool(anth),
        "masked":       _mask_key(anth),
        "source":       "config" if _val("anthropic_api_key") else ("env" if (os.getenv("ANTHROPIC_API_KEY") or "").strip() else "none"),
    }


@router.get("/ai-config")
def get_ai_config(db: Session = Depends(get_db), _=Depends(require_admin)):
    return _ai_status(db)


@router.post("/ai-config")
def set_ai_config(body: AIConfigIn, db: Session = Depends(get_db), user=Depends(require_admin)):
    fields = {"anthropic_api_key": body.api_key, "ai_provider": body.provider,
              "ai_api_key": body.ai_api_key, "ai_base_url": body.base_url, "ai_model": body.model}
    changed = []
    for key, value in fields.items():
        if value is None:
            continue
        row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if not row:
            row = SystemSetting(key=key)
            db.add(row)
        row.value = (value or "").strip()
        row.updated_by = user.username
        changed.append(key)
    try:
        db.add(AuditLog(user_id=user.id, username=user.username, action="ai_config_set",
                        action_type="human", entity_type="config", entity_id="ai_config",
                        detail=json.dumps({"changed": changed})))   # never log key material
    except Exception:
        pass
    db.commit()
    return _ai_status(db)
