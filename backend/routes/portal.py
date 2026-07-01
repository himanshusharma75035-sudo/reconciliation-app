"""
routes/portal.py — Developer Portal backend (Phase 1, additive, READ-ONLY).

Surfaces living codebase documentation, the DB schema, and the live API surface
for engineers, behind a dedicated `portal_access` permission (admins
short-circuit, as everywhere else). Every endpoint here is strictly read-only —
it introspects metadata and reads committed docs from disk. It NEVER writes to
or alters any recon/ingestion/config data, and it never touches the matching
engines. No new DB model or migration is required: `portal_access` is an opt-in
key in the existing per-user permissions JSON.

Live health / ingestion / audit data is intentionally NOT re-implemented here —
the portal frontend reuses the existing read-only endpoints
(/api/ingestion/*, /api/audit/logs, /api/insights/trend, /api/health).
"""
import os
import re
import time
import json
import datetime
import subprocess
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import (get_db, SessionLocal, generate_id, User, Base,
                             IngestionEvent, AuditLog,
                             AgentChatSession, AgentChatMessage, PortalRequest,
                             PortalRequestComment, PortalAgentJob, PortalAgentRun,
                             BuilderTask, BuilderMessage)
from core.auth import require_permission

router = APIRouter(prefix="/api/portal", tags=["developer-portal"])

# Gate the whole router on the new opt-in permission (admins bypass inside
# require_permission). Applied per-endpoint via Depends below.
_guard = require_permission("portal_access")
# Approving / rejecting requests needs the stronger 'portal_approve' permission
# (admins bypass). This is the human gate on acting upon a request.
_approve_guard = require_permission("portal_approve")
# The WRITE-CAPABLE Builder Agent (Phase 5) needs the strongest permission,
# 'portal_build' (admins bypass). Even with it, the master kill-switch
# (BUILDER_AGENT_ENABLED) and the mandatory gates still apply.
_build_guard = require_permission("portal_build")

APP_VERSION = "2.0.0"

# backend/routes/portal.py  ->  repo root is two levels up from this file's dir
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DOCS_DIR = os.path.join(_REPO_ROOT, "docs")


# ── Git / build metadata ──────────────────────────────────────────────────────

def _git(*args) -> Optional[str]:
    """Best-effort `git` call from the repo root. Returns None on any failure
    (e.g. git absent, not a checkout, deployed from a tarball)."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


@router.get("/meta")
def portal_meta(user: User = Depends(_guard)):
    """Deployment fingerprint so the docs always advertise which commit is live."""
    return {
        "app": "Eko Bharat Ventures — Reconciliation API",
        "version": APP_VERSION,
        "git": {
            "sha": _git("rev-parse", "HEAD"),
            "short_sha": _git("rev-parse", "--short", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "last_commit_date": _git("log", "-1", "--format=%cI"),
            "last_commit_subject": _git("log", "-1", "--format=%s"),
        },
    }


# ── "What changed" feed (live git history) ────────────────────────────────────
# Surfaces recent commits so the portal visibly reflects the deployed code, and
# flags commits that touched load-bearing files (behavior contract / data model)
# so engineers know to re-read the contract before trusting their mental model.

_SENSITIVE_PATHS = {
    "docs/behavior-contract.md": "behavior-contract",
    "backend/models/database.py": "data-model",
    "backend/routes/upload.py": "ingestion",
    "backend/core/matching_engine.py": "matching",
    "backend/core/evalue_engine.py": "matching",
}


@router.get("/changelog")
def changelog(limit: int = Query(30, ge=1, le=100), user: User = Depends(_guard)):
    """Recent commits with the files each touched, flagging contract/data-model
    changes. Always reflects the commit actually checked out — the portal's
    'does it self-update?' answer is yes: this reads live `git log`."""
    # One record per commit, fields unit-separated, commits NUL-separated.
    raw = _git("log", f"-{limit}", "--no-merges",
               "--pretty=format:%H%x1f%h%x1f%an%x1f%cI%x1f%s", "--name-only", "-z")
    if raw is None:
        return {"available": False, "commits": [], "reason": "git history is not available on this deployment."}

    commits = []
    # `-z --name-only` separates entries with NUL; the header line itself ends in \n.
    for block in raw.split("\x00\x00"):
        block = block.strip("\x00\n ")
        if not block:
            continue
        # The first \n splits the %x1f header from the NUL-joined file list.
        head, _, files_part = block.partition("\n")
        fields = head.split("\x1f")
        if len(fields) < 5:
            continue
        sha, short, author, date, subject = fields[:5]
        files = [f for f in files_part.split("\x00") if f.strip()]
        flags = sorted({tag for p, tag in _SENSITIVE_PATHS.items() if p in files})
        commits.append({
            "sha": sha, "short_sha": short, "author": author, "date": date,
            "subject": subject, "files_changed": len(files), "flags": flags,
        })

    flagged = sum(1 for c in commits if c["flags"])
    return {"available": True, "count": len(commits), "flagged_count": flagged,
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"), "commits": commits}


# ── Documentation (read committed docs/*.md) ──────────────────────────────────

# Friendly titles + ordering for the known docs; anything else is still listed.
_DOC_TITLES = {
    "onboarding.md": "Engineer Onboarding",
    "architecture.md": "Architecture",
    "behavior-contract.md": "Behavior Contract (25 invariants)",
    "known-issues.md": "Known Issues",
    "mysql-migration.md": "MySQL Migration Runbook",
    "audit-and-enhancement-roadmap.md": "Audit & Enhancement Roadmap",
}
_DOC_ORDER = list(_DOC_TITLES.keys())


def _list_doc_files() -> list[str]:
    if not os.path.isdir(_DOCS_DIR):
        return []
    files = [f for f in os.listdir(_DOCS_DIR)
             if f.lower().endswith(".md") and os.path.isfile(os.path.join(_DOCS_DIR, f))]
    # Known docs first (in curated order), then the rest alphabetically.
    known = [f for f in _DOC_ORDER if f in files]
    rest = sorted(f for f in files if f not in _DOC_ORDER)
    return known + rest


@router.get("/docs")
def list_docs(user: User = Depends(_guard)):
    """List the available documentation files (name + display title)."""
    files = _list_doc_files()
    return {
        "docs": [
            {"name": f, "title": _DOC_TITLES.get(f, f.replace(".md", "").replace("-", " ").title())}
            for f in files
        ]
    }


@router.get("/docs/{name}")
def get_doc(name: str, user: User = Depends(_guard)):
    """Return the raw markdown of a single doc. Path-traversal hardened: only
    plain *.md basenames inside docs/ are servable."""
    # Reject anything that isn't a bare filename ending in .md.
    if name != os.path.basename(name) or not name.lower().endswith(".md"):
        raise HTTPException(status_code=400, detail="Invalid document name")
    path = os.path.abspath(os.path.join(_DOCS_DIR, name))
    # Defence in depth: ensure the resolved path is still inside docs/.
    if os.path.commonpath([path, _DOCS_DIR]) != _DOCS_DIR or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Document not found")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except Exception:
        raise HTTPException(status_code=500, detail="Could not read document")
    return {
        "name": name,
        "title": _DOC_TITLES.get(name, name.replace(".md", "").replace("-", " ").title()),
        "content": content,
    }


# ── Database schema (introspect the ORM metadata, read-only) ──────────────────

def _col_type(col) -> str:
    try:
        return str(col.type)
    except Exception:
        return "?"


@router.get("/schema")
def db_schema(user: User = Depends(_guard)):
    """Auto-generated schema of every ORM-mapped table: columns, types, keys.
    Introspects SQLAlchemy metadata — touches no data."""
    tables = []
    for table_name in sorted(Base.metadata.tables.keys()):
        table = Base.metadata.tables[table_name]
        cols = []
        for col in table.columns:
            fks = sorted(
                f"{fk.column.table.name}.{fk.column.name}" for fk in col.foreign_keys
            )
            cols.append({
                "name": col.name,
                "type": _col_type(col),
                "nullable": bool(col.nullable),
                "primary_key": bool(col.primary_key),
                "index": bool(col.index),
                "unique": bool(col.unique) if col.unique is not None else False,
                "foreign_keys": fks,
                "default": str(col.default.arg) if col.default is not None and not callable(getattr(col.default, "arg", None)) else None,
            })
        tables.append({
            "name": table_name,
            "columns": cols,
            "column_count": len(cols),
        })
    return {"table_count": len(tables), "tables": tables}


# ── API surface (reflect the live OpenAPI spec) ───────────────────────────────

@router.get("/endpoints")
def api_endpoints(request: Request, user: User = Depends(_guard)):
    """Catalog of every HTTP endpoint, reflected from the running app's OpenAPI
    schema, grouped by tag. Always matches what's actually deployed."""
    try:
        spec = request.app.openapi()
    except Exception:
        raise HTTPException(status_code=500, detail="Could not build OpenAPI spec")

    by_tag: dict[str, list] = {}
    paths = spec.get("paths", {})
    for path, methods in sorted(paths.items()):
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            tags = op.get("tags") or ["untagged"]
            tag = tags[0]
            by_tag.setdefault(tag, []).append({
                "method": method.upper(),
                "path": path,
                "summary": (op.get("summary") or "").strip(),
            })

    groups = [
        {"tag": tag, "endpoints": eps}
        for tag, eps in sorted(by_tag.items())
    ]
    total = sum(len(g["endpoints"]) for g in groups)
    return {"endpoint_count": total, "tag_count": len(groups), "groups": groups}


# ── Live system health (read-only; self-contained under portal_access) ────────
# Wraps the existing read-only health helpers so a portal-only user does NOT
# also need the 'upload' permission that /api/ingestion/* requires.

@router.get("/health")
def portal_health(
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db),
    user: User = Depends(_guard),
):
    """Recon-health watchdog + ingestion rollup + DB reachability in one call."""
    from core.recon_health import compute_recon_health
    from models.database import DATABASE_URL, engine
    from sqlalchemy import text

    # Recon-health watchdog (already wraps every check in try/except internally).
    try:
        recon_health = compute_recon_health(db, days=days)
    except Exception as exc:  # never let the dashboard 500 on a watchdog hiccup
        recon_health = {"status": "unknown", "error": str(exc), "checks": []}

    # Ingestion rollup over the window (mirrors /api/ingestion/summary).
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    by_status: dict = {}
    by_channel: dict = {}
    rows_read = rows_accepted = rows_skipped = events = 0
    try:
        for e in db.query(IngestionEvent).filter(IngestionEvent.created_at >= since).all():
            events += 1
            by_status[e.status or "?"] = by_status.get(e.status or "?", 0) + 1
            by_channel[e.channel or "?"] = by_channel.get(e.channel or "?", 0) + 1
            rows_read += e.rows_read or 0
            rows_accepted += e.rows_accepted or 0
            rows_skipped += e.rows_skipped or 0
    except Exception:
        pass

    # Database reachability (mirrors /api/health).
    db_ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    return {
        "days": days,
        "system": {
            "version": APP_VERSION,
            "db": "mysql" if "mysql" in DATABASE_URL else "sqlite",
            "db_ok": db_ok,
        },
        "recon_health": recon_health,
        "ingestion_summary": {
            "events": events,
            "by_status": by_status,
            "by_channel": by_channel,
            "rows_read": rows_read,
            "rows_accepted": rows_accepted,
            "rows_skipped": rows_skipped,
        },
    }


# ── Recent audit activity (read-only; self-contained under portal_access) ─────

@router.get("/audit")
def portal_audit(
    limit: int = Query(50, ge=1, le=200),
    action_type: Optional[str] = None,   # "human" | "app"
    db: Session = Depends(get_db),
    user: User = Depends(_guard),
):
    """Most-recent audit-log entries for the portal's activity feed (read-only)."""
    def _jload(v):
        if v is None:
            return None
        try:
            return json.loads(v)
        except Exception:
            return v

    q = db.query(AuditLog)
    if action_type in ("human", "app"):
        q = q.filter(AuditLog.action_type == action_type)
    rows = q.order_by(AuditLog.created_at.desc()).limit(limit).all()
    return {
        "count": len(rows),
        "items": [{
            "id": r.id,
            "username": r.username,
            "action": r.action,
            "action_type": r.action_type,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "detail": _jload(r.detail),
            "created_at": str(r.created_at) if r.created_at else None,
        } for r in rows],
    }


# ── Developer Portal AI agent (Phase 2 — read-only chat) ──────────────────────
# The agent itself lives in core/portal_agent.py. It is strictly read-only and
# gated by the same `portal_access` permission. These endpoints persist the chat
# transcript and stream answers via Server-Sent Events.

class AgentChatBody(BaseModel):
    message: str
    session_id: Optional[str] = None


@router.get("/agent/status")
def agent_status(user: User = Depends(_guard)):
    """Whether the agent is configured (API key + SDK present) and which model."""
    from core import portal_agent
    return portal_agent.status()


@router.get("/agent/rules")
def agent_rules(user: User = Depends(_guard)):
    """The agent's current ground-rules (versioned markdown)."""
    from core import portal_agent
    try:
        with open(portal_agent._RULES_PATH, "r", encoding="utf-8") as fh:
            return {"content": fh.read()}
    except Exception:
        raise HTTPException(status_code=404, detail="Rules file not found")


# Curated one-click prompts so the agent isn't a blank box. Grouped by intent.
_PLAYBOOK = [
    {"group": "Understand the system", "items": [
        {"label": "Match-ID sequencing",
         "prompt": "Explain how reconciliation match IDs are generated and sequenced, and where this could break if the app ran with more than one worker. Cite the code."},
        {"label": "Ingestion → recon flow",
         "prompt": "Walk me through what happens from uploading a bank file to it being reconciled — the full pipeline, step by step, citing the routes and engines involved."},
        {"label": "Money tolerances",
         "prompt": "What are the per-engine money tolerances (core, E-Value, SBI, AePS, QR, BBPS) and why do they deliberately differ? Cite the behavior contract."},
        {"label": "The two ingest copies",
         "prompt": "Explain the two divergent ingest copies (interactive upload vs watch-folder) and what a fix in one would need mirrored in the other."},
    ]},
    {"group": "Investigate live data", "items": [
        {"label": "Open items by partner",
         "prompt": "How many open (unmatched) items are there per partner right now? Show the 10 oldest with their dates and amounts."},
        {"label": "Recent amount mismatches",
         "prompt": "Show the most recent amount_mismatch transactions and the size of each gap. Are any concentrated on one partner or date?"},
        {"label": "Low match-rate runs",
         "prompt": "Over the last 7 days, which reconciliation dates/partners had the lowest match rates? Summarise what stands out."},
    ]},
    {"group": "Operations & health", "items": [
        {"label": "Today's ingestion",
         "prompt": "Summarise today's ingestion events: counts by status and channel, rows read vs accepted vs skipped, and call out any failures."},
        {"label": "Anomaly scan → file a request",
         "prompt": "Scan the last 24h of ingestion and reconciliation for anomalies (failures, unusual skip rates, stuck open items). If you find something material, file a request describing it."},
    ]},
]


@router.get("/agent/playbook")
def agent_playbook(user: User = Depends(_guard)):
    """Curated starter prompts for the agent chat (static, no AI call)."""
    return {"groups": _PLAYBOOK}


@router.get("/agent/sessions")
def agent_sessions(db: Session = Depends(get_db), user: User = Depends(_guard)):
    """List the current user's chat threads (most recent first)."""
    rows = (db.query(AgentChatSession)
              .filter(AgentChatSession.username == user.username)
              .order_by(AgentChatSession.updated_at.desc())
              .limit(100).all())
    return {"sessions": [{
        "id": s.id, "title": s.title,
        "created_at": str(s.created_at) if s.created_at else None,
        "updated_at": str(s.updated_at) if s.updated_at else None,
    } for s in rows]}


@router.get("/agent/sessions/{sid}")
def agent_session_messages(sid: str, db: Session = Depends(get_db), user: User = Depends(_guard)):
    """Full transcript of one thread (owner only)."""
    s = db.query(AgentChatSession).filter(AgentChatSession.id == sid).first()
    if not s or s.username != user.username:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msgs = (db.query(AgentChatMessage)
              .filter(AgentChatMessage.session_id == sid)
              .order_by(AgentChatMessage.created_at.asc()).all())
    def _jload(v):
        try:
            return json.loads(v) if v else None
        except Exception:
            return None
    return {"id": s.id, "title": s.title, "messages": [{
        "role": m.role, "content": m.content,
        "tool_trace": _jload(m.tool_trace),
        "created_at": str(m.created_at) if m.created_at else None,
    } for m in msgs]}


@router.delete("/agent/sessions/{sid}")
def agent_delete_session(sid: str, db: Session = Depends(get_db), user: User = Depends(_guard)):
    s = db.query(AgentChatSession).filter(AgentChatSession.id == sid).first()
    if not s or s.username != user.username:
        raise HTTPException(status_code=404, detail="Conversation not found")
    db.query(AgentChatMessage).filter(AgentChatMessage.session_id == sid).delete()
    db.delete(s)
    db.commit()
    return {"deleted": sid}


@router.post("/agent/chat")
def agent_chat(body: AgentChatBody, db: Session = Depends(get_db), user: User = Depends(_guard)):
    """Stream an answer from the read-only agent as Server-Sent Events."""
    from core import portal_agent

    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty")
    if not portal_agent.is_enabled():
        raise HTTPException(status_code=503,
                            detail="The Developer Portal agent is not configured. "
                                   "Set ANTHROPIC_API_KEY in backend/.env and restart the backend.")

    # Resolve / create the thread and load prior history BEFORE adding this turn.
    history = []
    if body.session_id:
        s = db.query(AgentChatSession).filter(AgentChatSession.id == body.session_id).first()
        if not s or s.username != user.username:
            raise HTTPException(status_code=404, detail="Conversation not found")
        for m in (db.query(AgentChatMessage)
                    .filter(AgentChatMessage.session_id == s.id)
                    .order_by(AgentChatMessage.created_at.asc()).all()):
            history.append({"role": m.role, "content": m.content})
    else:
        s = AgentChatSession(id=generate_id(), username=user.username, title=message[:200],
                             created_at=datetime.datetime.utcnow(),
                             updated_at=datetime.datetime.utcnow())
        db.add(s)
        db.commit()

    # Persist this user turn now (request session).
    db.add(AgentChatMessage(id=generate_id(), session_id=s.id, role="user",
                            content=message, created_at=datetime.datetime.utcnow()))
    db.commit()
    sid = s.id
    username = user.username

    def _sse(obj) -> str:
        return f"data: {json.dumps(obj, default=str)}\n\n"

    def gen():
        yield _sse({"type": "session", "id": sid})
        answer = ""
        tool_trace = []
        errored = None
        try:
            for ev in portal_agent.stream_chat(history, message, actor=username, session_id=sid):
                if ev.get("type") == "done":
                    answer = ev.get("content", "")
                    tool_trace = ev.get("tool_trace", [])
                elif ev.get("type") == "error":
                    errored = ev.get("error")
                yield _sse(ev)
        except Exception as e:
            errored = str(e)
            yield _sse({"type": "error", "error": errored})

        # Persist the assistant turn + audit (fresh session — request db may be closed).
        try:
            wdb = SessionLocal()
            try:
                wdb.add(AgentChatMessage(
                    id=generate_id(), session_id=sid, role="assistant",
                    content=answer or (f"[error] {errored}" if errored else ""),
                    tool_trace=json.dumps(tool_trace) if tool_trace else None,
                    created_at=datetime.datetime.utcnow()))
                row = wdb.query(AgentChatSession).filter(AgentChatSession.id == sid).first()
                if row:
                    row.updated_at = datetime.datetime.utcnow()
                wdb.add(AuditLog(
                    id=generate_id(), username=username, action="portal_agent_query",
                    action_type="human", entity_type="agent_chat_session", entity_id=sid,
                    detail=json.dumps({"q": message[:300], "tools": tool_trace,
                                       "error": errored})[:2000],
                    created_at=datetime.datetime.utcnow()))
                wdb.commit()
            finally:
                wdb.close()
        except Exception:
            pass
        yield _sse({"type": "end"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Request / approval queue (Phase 3) ────────────────────────────────────────
# Filing a request changes nothing. A request becomes action only when a human
# with the 'portal_approve' permission moves it to 'approved'.

_REQ_TYPES = ("bug", "faulty_data", "feature", "change", "other")
_REQ_STATUSES = ("open", "triaged", "approved", "rejected", "done")


class PortalRequestCreate(BaseModel):
    type: str
    title: str
    description: str
    proposed_change: Optional[str] = None
    priority: Optional[str] = "medium"


class PortalRequestUpdate(BaseModel):
    status: Optional[str] = None       # triaged | approved | rejected | done
    priority: Optional[str] = None
    review_note: Optional[str] = None
    assignee: Optional[str] = None             # "" clears the assignee
    github_issue_url: Optional[str] = None     # "" clears the link


class PortalCommentBody(BaseModel):
    body: str


# SLA: how long an open/triaged request may sit before it's "overdue", by priority.
_SLA_HOURS = {"high": 24, "medium": 72, "low": 168}
_OPEN_STATUSES = ("open", "triaged")


def _age_and_sla(r: PortalRequest):
    """Return (age_hours, overdue) for an open request, computed server-side so the
    UI doesn't have to reason about the UTC→IST rewrite."""
    if not r.created_at:
        return None, False
    age_h = (datetime.datetime.utcnow() - r.created_at).total_seconds() / 3600.0
    overdue = r.status in _OPEN_STATUSES and age_h > _SLA_HOURS.get(r.priority or "medium", 72)
    return round(age_h, 1), overdue


def _request_row(r: PortalRequest, comment_count: int = 0) -> dict:
    age_h, overdue = _age_and_sla(r)
    return {
        "id": r.id, "type": r.req_type, "title": r.title, "description": r.description,
        "proposed_change": r.proposed_change, "priority": r.priority, "status": r.status,
        "source": r.source, "created_by": r.created_by, "agent_session_id": r.agent_session_id,
        "created_at": str(r.created_at) if r.created_at else None,
        "updated_at": str(r.updated_at) if r.updated_at else None,
        "reviewed_by": r.reviewed_by,
        "reviewed_at": str(r.reviewed_at) if r.reviewed_at else None,
        "review_note": r.review_note,
        "assignee": r.assignee,
        "github_issue_url": r.github_issue_url,
        "comment_count": comment_count,
        "age_hours": age_h,
        "overdue": overdue,
    }


@router.get("/requests")
def list_requests(
    status: Optional[str] = None,
    req_type: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(_guard),
):
    """The portal's request/approval queue (most recent first)."""
    q = db.query(PortalRequest)
    if status:
        q = q.filter(PortalRequest.status == status)
    if req_type:
        q = q.filter(PortalRequest.req_type == req_type)
    total = q.count()
    rows = (q.order_by(PortalRequest.created_at.desc())
              .offset((page - 1) * page_size).limit(page_size).all())
    # Lightweight status rollup for the queue header.
    counts = {}
    for (st,) in db.query(PortalRequest.status).all():
        counts[st or "?"] = counts.get(st or "?", 0) + 1
    # Comment counts for just the rows on this page.
    cc: dict = {}
    ids = [r.id for r in rows]
    if ids:
        from sqlalchemy import func
        for rid_, n in (db.query(PortalRequestComment.request_id, func.count(PortalRequestComment.id))
                          .filter(PortalRequestComment.request_id.in_(ids))
                          .group_by(PortalRequestComment.request_id).all()):
            cc[rid_] = n
    return {"total": total, "page": page, "page_size": page_size,
            "counts": counts, "items": [_request_row(r, cc.get(r.id, 0)) for r in rows]}


@router.get("/requests/{rid}")
def get_request(rid: str, db: Session = Depends(get_db), user: User = Depends(_guard)):
    r = db.query(PortalRequest).filter(PortalRequest.id == rid).first()
    if not r:
        raise HTTPException(status_code=404, detail="Request not found")
    n = db.query(PortalRequestComment).filter(PortalRequestComment.request_id == rid).count()
    return _request_row(r, n)


@router.post("/requests")
def create_request(body: PortalRequestCreate, db: Session = Depends(get_db), user: User = Depends(_guard)):
    """File a request manually (engineers). Agent-filed requests use the agent's tool."""
    rtype = (body.type or "other").strip().lower()
    if rtype not in _REQ_TYPES:
        raise HTTPException(status_code=400, detail=f"type must be one of {_REQ_TYPES}")
    title = (body.title or "").strip()
    description = (body.description or "").strip()
    if not title or not description:
        raise HTTPException(status_code=400, detail="title and description are required")
    priority = (body.priority or "medium").strip().lower()
    if priority not in ("low", "medium", "high"):
        priority = "medium"
    rid = generate_id()
    now = datetime.datetime.utcnow()
    db.add(PortalRequest(
        id=rid, req_type=rtype, title=title[:300], description=description,
        proposed_change=(body.proposed_change or None), priority=priority,
        status="open", source="manual", created_by=user.username,
        created_at=now, updated_at=now))
    db.add(AuditLog(id=generate_id(), username=user.username, action="portal_request_filed",
                    action_type="human", entity_type="portal_request", entity_id=rid,
                    detail=json.dumps({"type": rtype, "title": title[:200], "source": "manual"})[:2000],
                    created_at=now))
    db.commit()
    return {"id": rid, "status": "open"}


@router.patch("/requests/{rid}")
def update_request(rid: str, body: PortalRequestUpdate, db: Session = Depends(get_db),
                   user: User = Depends(_approve_guard)):
    """Approve / reject / re-prioritise a request. Requires 'portal_approve' (admins bypass).
    This is the human gate — it records the decision; it does not itself execute any change."""
    r = db.query(PortalRequest).filter(PortalRequest.id == rid).first()
    if not r:
        raise HTTPException(status_code=404, detail="Request not found")
    now = datetime.datetime.utcnow()
    changed = {}
    if body.priority is not None:
        p = body.priority.strip().lower()
        if p in ("low", "medium", "high"):
            r.priority = p
            changed["priority"] = p
    if body.status is not None:
        st = body.status.strip().lower()
        if st not in _REQ_STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of {_REQ_STATUSES}")
        r.status = st
        changed["status"] = st
        if st in ("approved", "rejected", "done"):
            r.reviewed_by = user.username
            r.reviewed_at = now
    if body.review_note is not None:
        r.review_note = body.review_note[:1000]
        changed["review_note"] = True
    if body.assignee is not None:
        r.assignee = (body.assignee.strip()[:100] or None)
        changed["assignee"] = r.assignee
    if body.github_issue_url is not None:
        url = body.github_issue_url.strip()[:500]
        r.github_issue_url = url or None
        changed["github_issue_url"] = r.github_issue_url
    r.updated_at = now
    db.add(AuditLog(id=generate_id(), username=user.username, action="portal_request_review",
                    action_type="human", entity_type="portal_request", entity_id=rid,
                    detail=json.dumps({"changed": changed, "title": r.title[:200]})[:2000],
                    created_at=now))
    db.commit()
    n = db.query(PortalRequestComment).filter(PortalRequestComment.request_id == rid).count()
    return _request_row(r, n)


@router.get("/requests/{rid}/comments")
def list_comments(rid: str, db: Session = Depends(get_db), user: User = Depends(_guard)):
    """Discussion thread on a request. Any portal user can read."""
    if not db.query(PortalRequest.id).filter(PortalRequest.id == rid).first():
        raise HTTPException(status_code=404, detail="Request not found")
    rows = (db.query(PortalRequestComment)
              .filter(PortalRequestComment.request_id == rid)
              .order_by(PortalRequestComment.created_at.asc()).all())
    return {"comments": [{
        "id": c.id, "author": c.author, "body": c.body,
        "created_at": str(c.created_at) if c.created_at else None,
    } for c in rows]}


@router.post("/requests/{rid}/comments")
def add_comment(rid: str, body: PortalCommentBody, db: Session = Depends(get_db),
                user: User = Depends(_guard)):
    """Add a discussion comment. Any portal user can comment (collaboration, not approval)."""
    r = db.query(PortalRequest).filter(PortalRequest.id == rid).first()
    if not r:
        raise HTTPException(status_code=404, detail="Request not found")
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Comment is empty")
    now = datetime.datetime.utcnow()
    cid = generate_id()
    db.add(PortalRequestComment(id=cid, request_id=rid, author=user.username,
                                body=text[:5000], created_at=now))
    r.updated_at = now
    db.add(AuditLog(id=generate_id(), username=user.username, action="portal_request_comment",
                    action_type="human", entity_type="portal_request", entity_id=rid,
                    detail=json.dumps({"title": r.title[:200]})[:2000], created_at=now))
    db.commit()
    return {"id": cid, "author": user.username, "body": text[:5000], "created_at": str(now)}


def _origin_https() -> Optional[str]:
    """The GitHub web URL of the `origin` remote (https or ssh form), or None."""
    url = _git("remote", "get-url", "origin")
    if not url:
        return None
    url = url.strip()
    m = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)        # git@github.com:owner/repo.git
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return re.sub(r"\.git$", "", url)                          # https://github.com/owner/repo(.git)


_GH_LABELS = {"bug": "bug", "faulty_data": "bug", "feature": "enhancement",
              "change": "enhancement", "other": ""}


@router.get("/requests/{rid}/github-issue")
def request_github_issue(rid: str, db: Session = Depends(get_db), user: User = Depends(_guard)):
    """Build a pre-filled GitHub 'new issue' URL for an approved request (no token
    needed — it just opens the GitHub compose page for a human to submit)."""
    r = db.query(PortalRequest).filter(PortalRequest.id == rid).first()
    if not r:
        raise HTTPException(status_code=404, detail="Request not found")
    base = _origin_https()
    if not base or "github.com" not in base:
        return {"url": None, "reason": "No GitHub 'origin' remote is configured for this checkout."}
    title = f"[{r.req_type}] {r.title}"
    parts = [r.description or ""]
    if r.proposed_change:
        parts.append("\n\n### Proposed change\n" + r.proposed_change)
    parts.append(f"\n\n---\n_Filed via the Developer Portal · request `{r.id}` · "
                 f"raised by {r.created_by} ({r.source})._")
    url = f"{base}/issues/new?title={quote(title)}&body={quote(''.join(parts))}"
    label = _GH_LABELS.get(r.req_type)
    if label:
        url += f"&labels={quote(label)}"
    return {"url": url}


# ── Scheduled agent (Phase 4) ─────────────────────────────────────────────────
# Recurring autonomous runs of the read-only agent. Managing jobs needs the
# 'portal_approve' permission (admins bypass); viewing needs 'portal_access'.

class PortalJobBody(BaseModel):
    name: str
    prompt: str
    frequency: Optional[str] = "daily"     # daily | weekly
    hour: Optional[int] = 8
    minute: Optional[int] = 0
    day_of_week: Optional[int] = None       # 0=Mon..6=Sun (weekly)
    is_enabled: Optional[bool] = True


class PortalJobUpdate(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    frequency: Optional[str] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
    day_of_week: Optional[int] = None
    is_enabled: Optional[bool] = None


def _job_row(j: PortalAgentJob) -> dict:
    return {
        "id": j.id, "name": j.name, "prompt": j.prompt, "frequency": j.frequency,
        "hour": j.hour, "minute": j.minute, "day_of_week": j.day_of_week,
        "is_enabled": j.is_enabled, "created_by": j.created_by,
        "created_at": str(j.created_at) if j.created_at else None,
        "last_run_at": str(j.last_run_at) if j.last_run_at else None,
        "last_status": j.last_status, "last_summary": j.last_summary,
    }


@router.get("/agent/jobs")
def list_jobs(db: Session = Depends(get_db), user: User = Depends(_guard)):
    rows = db.query(PortalAgentJob).order_by(PortalAgentJob.created_at.desc()).all()
    return {"jobs": [_job_row(j) for j in rows]}


@router.post("/agent/jobs")
def create_job(body: PortalJobBody, db: Session = Depends(get_db), user: User = Depends(_approve_guard)):
    name = (body.name or "").strip()
    prompt = (body.prompt or "").strip()
    if not name or not prompt:
        raise HTTPException(status_code=400, detail="name and prompt are required")
    freq = (body.frequency or "daily").strip().lower()
    if freq not in ("daily", "weekly"):
        freq = "daily"
    jid = generate_id()
    job = PortalAgentJob(
        id=jid, name=name[:200], prompt=prompt, frequency=freq,
        hour=max(0, min(23, body.hour if body.hour is not None else 8)),
        minute=max(0, min(59, body.minute if body.minute is not None else 0)),
        day_of_week=(body.day_of_week if freq == "weekly" else None),
        is_enabled=bool(body.is_enabled), created_by=user.username,
        created_at=datetime.datetime.utcnow())
    db.add(job)
    db.commit()
    try:
        from core import portal_scheduler
        portal_scheduler.register_job(job)
    except Exception:
        pass
    return _job_row(job)


@router.patch("/agent/jobs/{jid}")
def update_job(jid: str, body: PortalJobUpdate, db: Session = Depends(get_db),
               user: User = Depends(_approve_guard)):
    job = db.query(PortalAgentJob).filter(PortalAgentJob.id == jid).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if body.name is not None: job.name = body.name.strip()[:200]
    if body.prompt is not None: job.prompt = body.prompt.strip()
    if body.frequency is not None and body.frequency in ("daily", "weekly"): job.frequency = body.frequency
    if body.hour is not None: job.hour = max(0, min(23, body.hour))
    if body.minute is not None: job.minute = max(0, min(59, body.minute))
    if body.day_of_week is not None: job.day_of_week = body.day_of_week
    if body.is_enabled is not None: job.is_enabled = bool(body.is_enabled)
    db.commit()
    try:
        from core import portal_scheduler
        portal_scheduler.register_job(job)   # re-registers or removes if disabled
    except Exception:
        pass
    return _job_row(job)


@router.delete("/agent/jobs/{jid}")
def delete_job(jid: str, db: Session = Depends(get_db), user: User = Depends(_approve_guard)):
    job = db.query(PortalAgentJob).filter(PortalAgentJob.id == jid).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        from core import portal_scheduler
        portal_scheduler.unregister_job(jid)
    except Exception:
        pass
    db.query(PortalAgentRun).filter(PortalAgentRun.job_id == jid).delete()
    db.delete(job)
    db.commit()
    return {"deleted": jid}


@router.post("/agent/jobs/{jid}/run-now")
def run_job_now(jid: str, db: Session = Depends(get_db), user: User = Depends(_approve_guard)):
    job = db.query(PortalAgentJob).filter(PortalAgentJob.id == jid).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    import threading
    from core import portal_scheduler
    threading.Thread(target=portal_scheduler.run_job, args=(jid, "manual"), daemon=True).start()
    return {"status": "started"}


@router.get("/agent/jobs/{jid}/runs")
def job_runs(jid: str, limit: int = Query(20, ge=1, le=100),
             db: Session = Depends(get_db), user: User = Depends(_guard)):
    rows = (db.query(PortalAgentRun).filter(PortalAgentRun.job_id == jid)
              .order_by(PortalAgentRun.started_at.desc()).limit(limit).all())
    def _jload(v):
        try:
            return json.loads(v) if v else []
        except Exception:
            return []
    return {"runs": [{
        "id": r.id, "trigger": r.trigger, "status": r.status,
        "started_at": str(r.started_at) if r.started_at else None,
        "finished_at": str(r.finished_at) if r.finished_at else None,
        "summary": r.summary, "tools_used": _jload(r.tools_used),
        "requests_filed": r.requests_filed, "error": r.error,
    } for r in rows]}


# ── Builder Agent (Phase 5 — WRITE-CAPABLE, autonomous) ───────────────────────
# The agent that actually changes the app. Gated by 'portal_build' (admins bypass)
# AND the master kill-switch (BUILDER_AGENT_ENABLED, off by default). Every change
# must pass all gates before it can be applied; every applied change is revertible.

class BuilderChatBody(BaseModel):
    message: str
    task_id: Optional[str] = None
    title: Optional[str] = None
    plan_only: Optional[bool] = False


def _builder_task_row(t: BuilderTask) -> dict:
    def _j(v, d):
        try:
            return json.loads(v) if v else d
        except Exception:
            return d
    return {
        "id": t.id, "title": t.title, "instruction": t.instruction, "status": t.status,
        "branch": t.branch, "base_sha": t.base_sha, "commit_sha": t.commit_sha,
        "summary": t.summary, "plan": t.plan, "diff_stat": t.diff_stat,
        "files_changed": _j(t.files_changed, []), "gate_results": _j(t.gate_results, None),
        "gates_ok": bool(t.gates_ok), "created_by": t.created_by,
        "created_at": str(t.created_at) if t.created_at else None,
        "updated_at": str(t.updated_at) if t.updated_at else None,
        "applied_by": t.applied_by, "applied_at": str(t.applied_at) if t.applied_at else None,
        "rolled_back_by": t.rolled_back_by,
        "rolled_back_at": str(t.rolled_back_at) if t.rolled_back_at else None,
        "error": t.error,
    }


@router.get("/builder/status")
def builder_status(user: User = Depends(_build_guard)):
    """Master-switch + model + auto-apply state for the Builder Agent."""
    from core import builder_agent
    return builder_agent.status()


@router.get("/builder/tasks")
def builder_tasks(db: Session = Depends(get_db), user: User = Depends(_build_guard)):
    rows = db.query(BuilderTask).order_by(BuilderTask.created_at.desc()).limit(100).all()
    return {"tasks": [_builder_task_row(t) for t in rows]}


@router.get("/builder/tasks/{tid}")
def builder_task_detail(tid: str, db: Session = Depends(get_db), user: User = Depends(_build_guard)):
    t = db.query(BuilderTask).filter(BuilderTask.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    msgs = (db.query(BuilderMessage).filter(BuilderMessage.task_id == tid)
              .order_by(BuilderMessage.created_at.asc()).all())
    def _j(v):
        try:
            return json.loads(v) if v else None
        except Exception:
            return None
    return {**_builder_task_row(t), "messages": [{
        "role": m.role, "content": m.content, "tool_trace": _j(m.tool_trace),
        "created_at": str(m.created_at) if m.created_at else None,
    } for m in msgs]}


@router.delete("/builder/tasks/{tid}")
def builder_delete_task(tid: str, db: Session = Depends(get_db), user: User = Depends(_build_guard)):
    """Delete a task + its conversation. Refuses if the task is applied and not rolled back
    (roll it back first, so live code is never orphaned from its task record)."""
    t = db.query(BuilderTask).filter(BuilderTask.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    if t.status == "applied":
        raise HTTPException(status_code=409, detail="Task is applied to live code. Roll it back before deleting.")
    db.query(BuilderMessage).filter(BuilderMessage.task_id == tid).delete()
    db.delete(t)
    db.commit()
    # Best-effort cleanup of the backup snapshot (only when not applied).
    try:
        import shutil
        from core import builder_agent
        shutil.rmtree(builder_agent._backup_dir(tid), ignore_errors=True)
    except Exception:
        pass
    return {"deleted": tid}


@router.post("/builder/chat")
def builder_chat(body: BuilderChatBody, db: Session = Depends(get_db), user: User = Depends(_build_guard)):
    """Drive a build via Server-Sent Events. Creates/loads a task, streams the
    agent's plan → edits → gate results → finish, persisting everything."""
    from core import builder_agent

    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty")
    if not builder_agent._master_on():
        raise HTTPException(status_code=503,
                            detail="Builder Agent is OFF. Set BUILDER_AGENT_ENABLED=1 in backend/.env and restart.")
    if not builder_agent.is_enabled():
        raise HTTPException(status_code=503,
                            detail="Builder Agent is enabled but the LLM is not configured (ANTHROPIC_API_KEY / credits).")

    # Resolve / create the task and load prior conversation.
    history = []
    if body.task_id:
        t = db.query(BuilderTask).filter(BuilderTask.id == body.task_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="Task not found")
        if t.status == "applied":
            raise HTTPException(status_code=409, detail="This task is already applied. Start a new task for further changes.")
        for m in (db.query(BuilderMessage).filter(BuilderMessage.task_id == t.id)
                    .filter(BuilderMessage.role.in_(("user", "assistant")))
                    .order_by(BuilderMessage.created_at.asc()).all()):
            history.append({"role": m.role, "content": m.content})
    else:
        base = _git("rev-parse", "HEAD")
        t = BuilderTask(id=generate_id(), title=(body.title or message)[:300], instruction=message,
                        status="planning", base_sha=base, branch=None, created_by=user.username,
                        created_at=datetime.datetime.utcnow(), updated_at=datetime.datetime.utcnow())
        db.add(t)
        db.commit()

    db.add(BuilderMessage(id=generate_id(), task_id=t.id, role="user", content=message,
                          created_at=datetime.datetime.utcnow()))
    db.commit()
    tid = t.id
    username = user.username

    def _sse(obj) -> str:
        return f"data: {json.dumps(obj, default=str)}\n\n"

    def gen():
        yield _sse({"type": "task", "id": tid})
        answer, trace, changed, status_, gates, summary, plan = "", [], [], "building", None, None, None
        try:
            for ev in builder_agent.stream_build(history, message, tid, actor=username,
                                                 plan_only=bool(body.plan_only)):
                et = ev.get("type")
                if et == "plan":
                    plan = ev.get("plan")
                elif et == "done":
                    answer = ev.get("content", ""); trace = ev.get("tool_trace", [])
                    changed = ev.get("changed", []); status_ = ev.get("status", "building")
                    gates = ev.get("gates"); summary = ev.get("summary")
                yield _sse(ev)
        except Exception as e:
            status_ = "failed"
            yield _sse({"type": "error", "error": str(e)})

        # Persist the assistant turn + task state (fresh session).
        try:
            wdb = SessionLocal()
            try:
                wdb.add(BuilderMessage(id=generate_id(), task_id=tid, role="assistant",
                                       content=answer or (summary or ""),
                                       tool_trace=json.dumps(trace) if trace else None,
                                       created_at=datetime.datetime.utcnow()))
                row = wdb.query(BuilderTask).filter(BuilderTask.id == tid).first()
                if row:
                    row.updated_at = datetime.datetime.utcnow()
                    row.status = status_
                    if summary:
                        row.summary = summary
                    if plan:
                        row.plan = plan
                    if changed:
                        row.files_changed = json.dumps(changed)
                        row.diff_stat = builder_agent.diff_stat(changed)
                    if gates:
                        row.gate_results = json.dumps(gates.get("gates") if isinstance(gates, dict) else gates)
                        row.gates_ok = bool(gates.get("ok")) if isinstance(gates, dict) else False
                    # Extract the plan from the stream if present (last plan event).
                wdb.add(AuditLog(id=generate_id(), username=username, action="builder_turn",
                                 action_type="human", entity_type="builder_task", entity_id=tid,
                                 detail=json.dumps({"status": status_, "changed": changed[:50],
                                                    "tools": trace[:50]})[:2000],
                                 created_at=datetime.datetime.utcnow()))
                wdb.commit()
            finally:
                wdb.close()
        except Exception:
            pass

        # Auto-apply: fully-autonomous mode. If the change is vetted (gates ok) and
        # BUILDER_AUTO_APPLY is on, apply it now — UNLESS it touches the money-matching
        # core, which always needs a human click (protected-core escalation).
        gates_ok = bool(gates and (gates.get("ok") if isinstance(gates, dict) else False))
        if status_ == "ready" and gates_ok and builder_agent._auto_apply():
            core = builder_agent.touches_core(changed)
            if core:
                yield _sse({"type": "escalation", "reason": "core", "files": core,
                            "message": "Change touches the money-matching/ingestion core — "
                                       "auto-apply is held; a human must click Apply."})
            else:
                res = builder_agent.apply_task(tid, username, auto=True)
                yield _sse({"type": "applied", "result": res})
        yield _sse({"type": "end"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/builder/tasks/{tid}/diff")
def builder_task_diff(tid: str, db: Session = Depends(get_db), user: User = Depends(_build_guard)):
    """Unified diff of the task's changes vs the pre-change backups (for the diff viewer)."""
    t = db.query(BuilderTask).filter(BuilderTask.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    from core import builder_agent
    manifest = builder_agent._load_manifest(tid)
    diffs = []
    import difflib
    for rel, info in manifest.items():
        abs_path = os.path.join(builder_agent._REPO_ROOT, rel)
        try:
            new = open(abs_path, "r", encoding="utf-8", errors="replace").read().splitlines() if os.path.isfile(abs_path) else []
        except Exception:
            new = []
        if info.get("existed"):
            bpath = os.path.join(builder_agent._backup_dir(tid), "files", rel)
            old = open(bpath, "r", encoding="utf-8", errors="replace").read().splitlines() if os.path.isfile(bpath) else []
            action = "deleted" if not os.path.isfile(abs_path) else "modified"
        else:
            old, action = [], "created"
        ud = list(difflib.unified_diff(old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
        diffs.append({"path": rel, "action": action, "diff": "\n".join(ud[:1200])})
    return {"files": diffs}


@router.post("/builder/tasks/{tid}/apply")
def builder_apply(tid: str, db: Session = Depends(get_db), user: User = Depends(_build_guard)):
    """Make a vetted change live: commit it (audit + rollback point) and mark applied.
    Refuses unless the gates passed. Backend code changes take effect on the next
    backend restart (use /builder/restart); frontend changes are already built by the
    gate. The remote real-money server is NOT touched from here."""
    from core import builder_agent
    t = db.query(BuilderTask).filter(BuilderTask.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    if not builder_agent._master_on():
        raise HTTPException(status_code=503, detail="Builder Agent master switch is OFF.")
    if not t.gates_ok:
        raise HTTPException(status_code=409, detail="Gates have not passed for this task — cannot apply.")
    if t.status == "applied":
        return {"status": "applied", "commit_sha": t.commit_sha, "note": "already applied"}
    res = builder_agent.apply_task(tid, user.username, auto=False)
    if res.get("error"):
        raise HTTPException(status_code=409, detail=res["error"])
    res["note"] = "Change committed and health watchdog armed. Backend code changes load on the next restart."
    return res


@router.post("/builder/tasks/{tid}/rollback")
def builder_rollback(tid: str, db: Session = Depends(get_db), user: User = Depends(_build_guard)):
    """Restore the task's files to their exact pre-change state (reverses an apply too)."""
    from core import builder_agent
    t = db.query(BuilderTask).filter(BuilderTask.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    result = builder_agent.restore_backups(tid)
    now = datetime.datetime.utcnow()
    prev = t.status
    t.status = "rolled_back"
    t.rolled_back_by = user.username
    t.rolled_back_at = now
    t.updated_at = now
    changed = []
    try:
        changed = json.loads(t.files_changed) if t.files_changed else []
    except Exception:
        pass
    # If it had been committed, record a revert commit for the restored files.
    if prev == "applied" and changed:
        builder_agent.git_commit(changed, f"builder: rollback of task {t.id} by {user.username}")
    db.add(AuditLog(id=generate_id(), username=user.username, action="builder_rollback",
                    action_type="human", entity_type="builder_task", entity_id=tid,
                    detail=json.dumps({"title": t.title[:200], "result": result})[:2000],
                    created_at=now))
    db.commit()
    return {"status": "rolled_back", "result": result,
            "restart_needed": any(str(c).startswith("backend/") for c in changed)}


@router.post("/builder/restart")
def builder_restart(user: User = Depends(_build_guard)):
    """Best-effort restart of THIS backend process so applied backend code loads.
    Spawns a detached relauncher that waits for this process to exit, then restarts
    it with the same command line, and returns before exiting. If it can't determine
    how it was launched, it reports so and leaves the process running untouched."""
    from core import builder_agent
    if not builder_agent._master_on():
        raise HTTPException(status_code=503, detail="Builder Agent master switch is OFF.")
    ok, detail = _spawn_relauncher()
    return {"restarting": ok, "detail": detail}


def _spawn_relauncher():
    """Windows/py: launch a detached child that waits for our PID to die then re-runs
    our argv. Returns (scheduled: bool, detail: str). Does not itself exit the process —
    a separate short-delay thread does, so the HTTP response can flush first."""
    import sys
    import threading
    pid = os.getpid()
    argv = [sys.executable, *sys.argv]
    # Only attempt when we look like a uvicorn launch (avoid restarting a test runner).
    if not any("uvicorn" in str(a) for a in sys.argv):
        return False, "Could not identify a uvicorn launch command; restart the backend manually."
    try:
        cmd = " ".join(f'"{a}"' if " " in str(a) else str(a) for a in argv)
        # PowerShell: wait for our PID to exit, then relaunch in the backend dir.
        ps = (f"try {{ Wait-Process -Id {pid} -Timeout 60 }} catch {{}}; "
              f"Set-Location -LiteralPath '{_REPO_ROOT}\\backend'; "
              f"Start-Process -FilePath '{argv[0]}' -ArgumentList @({', '.join(repr(a) for a in sys.argv)}) "
              f"-WorkingDirectory '{_REPO_ROOT}\\backend'")
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                         creationflags=0x00000008)  # DETACHED_PROCESS
        threading.Thread(target=lambda: (time.sleep(1.5), os._exit(0)), daemon=True).start()
        return True, "Relauncher scheduled; backend will restart momentarily."
    except Exception as e:
        return False, f"Could not schedule restart: {e}"


@router.get("/builder/playbook")
def builder_playbook(user: User = Depends(_build_guard)):
    """Curated one-click build tasks (test-bootstrap first). Static, no AI call."""
    from core import builder_agent
    return {"groups": builder_agent.PLAYBOOK}


@router.get("/builder/deploy/status")
def builder_deploy_status(user: User = Depends(_build_guard)):
    from core import builder_agent
    return {"available": builder_agent.deploy_available()}


@router.post("/builder/deploy")
def builder_deploy(user: User = Depends(_build_guard)):
    """Run the configured remote-deploy command (build + scp + restart the RAG server).
    Finishes the 'to prod' loop, but only if BUILDER_DEPLOY_COMMAND is configured —
    the app never embeds server credentials."""
    from core import builder_agent
    if not builder_agent._master_on():
        raise HTTPException(status_code=503, detail="Builder Agent master switch is OFF.")
    res = builder_agent.run_deploy()
    return res


# ── Read-only data explorer (guarded SQL, same engine the agent uses) ──────────

class SqlBody(BaseModel):
    query: str


@router.post("/sql")
def data_explorer(body: SqlBody, user: User = Depends(_guard)):
    """Run one read-only SELECT against the live DB — same SELECT-only guard, row cap,
    blocked-tables and read-only-transaction rails as the agent's run_sql tool."""
    from core import portal_agent
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="Query is empty")
    try:
        result = portal_agent._run_sql(q)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Query error: {e}")
    # Audit the read (same spirit as the agent's queries).
    try:
        db = SessionLocal()
        db.add(AuditLog(id=generate_id(), username=user.username, action="portal_sql_query",
                        action_type="human", entity_type="data_explorer", entity_id=None,
                        detail=json.dumps({"q": q[:300], "rows": result.get("row_count")})[:2000],
                        created_at=datetime.datetime.utcnow()))
        db.commit(); db.close()
    except Exception:
        pass
    return result


# ── Portal / agent metrics ─────────────────────────────────────────────────────

@router.get("/metrics")
def portal_metrics(db: Session = Depends(get_db), user: User = Depends(_guard)):
    """Roll-up of portal activity: requests, agent queries, and builder tasks."""
    from sqlalchemy import func

    def _counts(model, col):
        out = {}
        for val, n in db.query(col, func.count(model.id)).group_by(col).all():
            out[val or "?"] = n
        return out

    req_by_status = _counts(PortalRequest, PortalRequest.status)
    req_by_type = _counts(PortalRequest, PortalRequest.req_type)
    builder_by_status = _counts(BuilderTask, BuilderTask.status)

    agent_queries = db.query(AuditLog).filter(AuditLog.action == "portal_agent_query").count()
    builder_applies = db.query(AuditLog).filter(AuditLog.action == "builder_apply").count()
    builder_rollbacks = db.query(AuditLog).filter(
        AuditLog.action.in_(("builder_rollback", "builder_auto_rollback"))).count()
    auto_rollbacks = db.query(AuditLog).filter(AuditLog.action == "builder_auto_rollback").count()

    return {
        "requests": {"total": sum(req_by_status.values()), "by_status": req_by_status, "by_type": req_by_type},
        "agent": {"queries": agent_queries},
        "builder": {
            "tasks_by_status": builder_by_status,
            "tasks_total": sum(builder_by_status.values()),
            "applies": builder_applies,
            "rollbacks": builder_rollbacks,
            "auto_rollbacks": auto_rollbacks,
        },
    }
