"""
core/portal_agent.py — Developer Portal AI agent (Phase 2).

A READ-ONLY Claude assistant for engineers, gated by the 'portal_access'
permission. It can read the codebase docs, the ORM schema, source files, and run
SELECT-only queries against the live DB. It has NO write capability of any kind —
there is deliberately no tool that can INSERT/UPDATE/DELETE/ALTER or write a file.

Safety rails (automated, not human-gated):
  • run_sql accepts a single SELECT/WITH statement only; INSERT/UPDATE/DELETE/DDL
    and multi-statement input are rejected before execution.
  • The `users` and `api_keys` tables (password hashes, key hashes) are blocked.
  • Every query is row-capped and executed in a read-only / rolled-back transaction.
  • read_file is confined to the repo tree and refuses .env / secret / db files.

The Anthropic SDK is imported lazily so the app still boots when the package or
the ANTHROPIC_API_KEY is absent (the agent simply reports as disabled).
"""
import os
import re
import json
import logging
import datetime

logger = logging.getLogger("eko_recon.portal_agent")

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DOCS_DIR = os.path.join(_REPO_ROOT, "docs")
_RULES_PATH = os.path.join(os.path.dirname(__file__), "portal_agent_rules.md")

# Tables the agent's SQL tool may never touch (secrets / auth material).
_BLOCKED_TABLES = {"users", "api_keys"}

SQL_ROW_CAP = 500          # hard cap on rows returned to the model
SQL_TIMEOUT_MS = 8000      # best-effort statement timeout (MySQL)
READ_FILE_MAX_BYTES = 120_000
SEARCH_MAX_HITS = 60


# ── Availability ──────────────────────────────────────────────────────────────

def _api_key() -> str:
    return (os.getenv("ANTHROPIC_API_KEY") or "").strip()


def is_enabled() -> bool:
    """True when the agent can run (SDK importable + key configured)."""
    if not _api_key():
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def status() -> dict:
    has_key = bool(_api_key())
    try:
        import anthropic  # noqa: F401
        has_sdk = True
    except Exception:
        has_sdk = False
    return {
        "enabled": has_key and has_sdk,
        "has_key": has_key,
        "has_sdk": has_sdk,
        "model": os.getenv("PORTAL_AGENT_MODEL", "claude-opus-4-8"),
        "effort": os.getenv("PORTAL_AGENT_EFFORT", "high"),
    }


# ── System prompt ─────────────────────────────────────────────────────────────

def _load_rules() -> str:
    try:
        with open(_RULES_PATH, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return "You are the read-only Eko Recon Developer Portal agent."


def _build_system() -> str:
    rules = _load_rules()
    tables = ", ".join(sorted(__import__("models.database", fromlist=["Base"]).Base.metadata.tables.keys()))
    return (
        rules
        + "\n\n---\n## Live context\n"
        + f"Database tables available to read_schema/run_sql (except blocked auth tables): {tables}\n"
        + "Use the tools to ground every answer. Today's date is "
        + datetime.date.today().isoformat()
        + " (server local)."
    )


# ── Tool definitions (Anthropic tool schema) ──────────────────────────────────

TOOLS = [
    {
        "name": "run_sql",
        "description": (
            "Run ONE read-only SQL query against the live reconciliation database "
            "and return rows. SELECT or WITH...SELECT only — any INSERT/UPDATE/DELETE/"
            "DDL or multi-statement input is rejected. Results are row-capped at "
            f"{SQL_ROW_CAP}. The `users` and `api_keys` tables are not queryable. "
            "Prefer this for questions about live data (counts, sums, open items, "
            "match rates, specific transactions)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A single read-only SQL SELECT statement."},
                "purpose": {"type": "string", "description": "One line: why you are running this."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_schema",
        "description": (
            "Introspect the database schema. With no table name, returns the list of "
            "tables and their column counts. With a table name, returns its columns "
            "(name, type, nullable, primary key, foreign keys)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "Optional table name."}},
        },
    },
    {
        "name": "read_doc",
        "description": "Read one of the project documentation markdown files (docs/*.md). Call with no name to list them.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Doc filename, e.g. behavior-contract.md"}},
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a source file from the repository by relative path (e.g. "
            "backend/core/matching_engine.py). Confined to the repo; refuses .env, "
            "databases, and secret files. Returns up to ~120KB with line numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repo-relative file path."}},
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": (
            "Regex-search the source tree (backend/, frontend/src/, docs/) and return "
            "matching file:line snippets. Use to locate code before reading it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression."},
                "path_glob": {"type": "string", "description": "Optional substring to filter file paths, e.g. 'routes/'."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "file_request",
        "description": (
            "File a change/error/feature request into the portal's approval queue. "
            "This does NOT change anything in the live system — it records a proposal "
            "that a human with approval rights must approve before any action is taken. "
            "Use this when the user reports a bug, flags faulty data, or asks for a "
            "feature or change. Fill in a clear title, a detailed description grounded "
            "in what you found (cite files/tables), and a concrete proposed_change. "
            "Always tell the user you filed it and that it awaits human approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["bug", "faulty_data", "feature", "change", "other"]},
                "title": {"type": "string", "description": "Short, specific title."},
                "description": {"type": "string", "description": "Detailed context, grounded in files/tables you inspected."},
                "proposed_change": {"type": "string", "description": "Concrete proposed fix/change/plan. Optional."},
                "priority": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["type", "title", "description"],
        },
    },
]


# ── SQL guard ─────────────────────────────────────────────────────────────────

_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|replace|grant|revoke|"
    r"attach|detach|pragma|merge|call|exec|execute|into|vacuum|reindex)\b",
    re.IGNORECASE,
)


def _strip_sql_comments(q: str) -> str:
    q = re.sub(r"/\*.*?\*/", " ", q, flags=re.DOTALL)   # /* block */
    q = re.sub(r"--[^\n]*", " ", q)                      # -- line
    return q


def _validate_sql(query: str) -> str:
    """Return cleaned query or raise ValueError. SELECT/WITH only, single statement."""
    if not query or not query.strip():
        raise ValueError("Empty query.")
    cleaned = _strip_sql_comments(query).strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("Query is empty after removing comments.")
    if ";" in cleaned:
        raise ValueError("Only a single statement is allowed (no ';').")
    low = cleaned.lower()
    if not (low.startswith("select") or low.startswith("with")):
        raise ValueError("Only SELECT (or WITH ... SELECT) queries are allowed.")
    if _SQL_FORBIDDEN.search(cleaned):
        raise ValueError("Query contains a disallowed keyword (write/DDL/INTO/etc.).")
    for t in _BLOCKED_TABLES:
        if re.search(rf"\b{re.escape(t)}\b", cleaned, re.IGNORECASE):
            raise ValueError(f"The '{t}' table is not queryable from the agent.")
    return cleaned


def _ensure_limit(cleaned: str) -> str:
    if re.search(r"\blimit\b", cleaned, re.IGNORECASE):
        return cleaned
    return f"{cleaned}\nLIMIT {SQL_ROW_CAP}"


def _jsonable(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return str(v)
    try:
        json.dumps(v)
        return v
    except Exception:
        return str(v)


def _run_sql(query: str) -> dict:
    cleaned = _ensure_limit(_validate_sql(query))
    from models.database import engine, DATABASE_URL
    from sqlalchemy import text as _text

    is_mysql = "mysql" in (DATABASE_URL or "")
    conn = engine.connect()
    try:
        # Defence in depth: read-only / query-only at the driver level.
        try:
            if is_mysql:
                conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME={SQL_TIMEOUT_MS}")
                conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            else:  # sqlite
                conn.exec_driver_sql("PRAGMA query_only = ON")
        except Exception:
            pass  # best-effort; LIMIT + rollback still apply

        result = conn.execute(_text(cleaned))
        cols = list(result.keys())
        rows = []
        for i, r in enumerate(result):
            if i >= SQL_ROW_CAP:
                break
            rows.append([_jsonable(v) for v in r])
        return {"columns": cols, "rows": rows, "row_count": len(rows),
                "truncated": len(rows) >= SQL_ROW_CAP, "query": cleaned}
    finally:
        try:
            conn.rollback()   # never persist anything
        except Exception:
            pass
        conn.close()


# ── Other read-only tools ─────────────────────────────────────────────────────

def _read_schema(table: str = None) -> dict:
    from models.database import Base
    md = Base.metadata
    if not table:
        return {"tables": [
            {"name": n, "columns": len(md.tables[n].columns)}
            for n in sorted(md.tables.keys()) if n not in _BLOCKED_TABLES
        ]}
    if table in _BLOCKED_TABLES or table not in md.tables:
        return {"error": f"Table '{table}' is not available."}
    t = md.tables[table]
    cols = []
    for c in t.columns:
        cols.append({
            "name": c.name, "type": str(c.type), "nullable": bool(c.nullable),
            "primary_key": bool(c.primary_key),
            "foreign_keys": sorted(f"{fk.column.table.name}.{fk.column.name}" for fk in c.foreign_keys),
        })
    return {"table": table, "columns": cols}


def _list_docs() -> list:
    if not os.path.isdir(_DOCS_DIR):
        return []
    return sorted(f for f in os.listdir(_DOCS_DIR) if f.lower().endswith(".md"))


def _read_doc(name: str = None) -> dict:
    if not name:
        return {"docs": _list_docs()}
    if name != os.path.basename(name) or not name.lower().endswith(".md"):
        return {"error": "Invalid document name."}
    path = os.path.abspath(os.path.join(_DOCS_DIR, name))
    if os.path.commonpath([path, _DOCS_DIR]) != _DOCS_DIR or not os.path.isfile(path):
        return {"error": "Document not found."}
    with open(path, "r", encoding="utf-8") as fh:
        return {"name": name, "content": fh.read()}


_READ_FILE_BLOCK = re.compile(r"(^|/)(\.env|.*\.db|.*\.sqlite3?|seed_accounts\.json)$", re.IGNORECASE)
_READ_FILE_ALLOW_EXT = {
    ".py", ".jsx", ".js", ".ts", ".tsx", ".md", ".json", ".css", ".html",
    ".txt", ".yml", ".yaml", ".bat", ".sh", ".example", ".cfg", ".ini", ".toml", "",
}


def _read_file(path: str) -> dict:
    rel = (path or "").strip().lstrip("/\\")
    target = os.path.abspath(os.path.join(_REPO_ROOT, rel))
    if os.path.commonpath([target, _REPO_ROOT]) != _REPO_ROOT:
        return {"error": "Path escapes the repository."}
    norm = target.replace("\\", "/")
    if "/.git/" in norm or "/node_modules/" in norm or "/instance/" in norm or "/dist/" in norm:
        return {"error": "That location is not readable from the agent."}
    if _READ_FILE_BLOCK.search(norm):
        return {"error": "Refusing to read a secret/database file."}
    if os.path.splitext(target)[1].lower() not in _READ_FILE_ALLOW_EXT:
        return {"error": "Unsupported file type."}
    if not os.path.isfile(target):
        return {"error": "File not found."}
    if os.path.getsize(target) > READ_FILE_MAX_BYTES:
        return {"error": f"File too large (> {READ_FILE_MAX_BYTES} bytes); narrow with search_code."}
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    numbered = "\n".join(f"{i+1}\t{ln}" for i, ln in enumerate(lines))
    return {"path": rel, "lines": len(lines), "content": numbered}


_SEARCH_DIRS = ("backend", "frontend/src", "docs")
_SEARCH_SKIP = ("node_modules", "__pycache__", ".git", "dist", "instance")
_SEARCH_EXT = {".py", ".jsx", ".js", ".ts", ".tsx", ".md", ".css", ".json"}


def _search_code(pattern: str, path_glob: str = None) -> dict:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"error": f"Invalid regex: {e}"}
    hits = []
    for base in _SEARCH_DIRS:
        root = os.path.join(_REPO_ROOT, base)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SEARCH_SKIP]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in _SEARCH_EXT:
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, _REPO_ROOT).replace("\\", "/")
                if path_glob and path_glob not in rel:
                    continue
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        for n, line in enumerate(fh, 1):
                            if rx.search(line):
                                hits.append({"file": rel, "line": n, "text": line.rstrip()[:240]})
                                if len(hits) >= SEARCH_MAX_HITS:
                                    return {"hits": hits, "truncated": True}
                except Exception:
                    continue
    return {"hits": hits, "truncated": False}


def _file_request(args: dict, ctx: dict) -> dict:
    """Insert a PortalRequest (status 'open') — a proposal awaiting human approval.
    Writes ONLY to the isolated governance table, never to business data."""
    from models.database import SessionLocal, generate_id, PortalRequest, AuditLog
    req_type = (args.get("type") or "other").strip().lower()
    if req_type not in ("bug", "faulty_data", "feature", "change", "other"):
        req_type = "other"
    title = (args.get("title") or "").strip()[:300]
    description = (args.get("description") or "").strip()
    if not title or not description:
        return {"error": "A request needs both a title and a description."}
    priority = (args.get("priority") or "medium").strip().lower()
    if priority not in ("low", "medium", "high"):
        priority = "medium"
    actor = (ctx or {}).get("actor") or "agent"
    sid = (ctx or {}).get("session_id")
    rid = generate_id()
    db = SessionLocal()
    try:
        db.add(PortalRequest(
            id=rid, req_type=req_type, title=title, description=description,
            proposed_change=(args.get("proposed_change") or None), priority=priority,
            status="open", source="agent", created_by=actor, agent_session_id=sid,
            created_at=datetime.datetime.utcnow(), updated_at=datetime.datetime.utcnow()))
        db.add(AuditLog(
            id=generate_id(), username=actor, action="portal_request_filed",
            action_type="human", entity_type="portal_request", entity_id=rid,
            detail=json.dumps({"type": req_type, "title": title, "source": "agent"})[:2000],
            created_at=datetime.datetime.utcnow()))
        db.commit()
    finally:
        db.close()
    return {"request_id": rid, "status": "open", "type": req_type, "title": title,
            "note": "Filed into the approval queue. It changes nothing until a human approves it."}


def _execute_tool(name: str, args: dict, ctx: dict = None) -> dict:
    try:
        if name == "run_sql":
            return _run_sql(args.get("query", ""))
        if name == "read_schema":
            return _read_schema(args.get("table"))
        if name == "read_doc":
            return _read_doc(args.get("name"))
        if name == "read_file":
            return _read_file(args.get("path", ""))
        if name == "search_code":
            return _search_code(args.get("pattern", ""), args.get("path_glob"))
        if name == "file_request":
            return _file_request(args, ctx or {})
        return {"error": f"Unknown tool '{name}'."}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.warning("portal agent tool '%s' failed: %s", name, e)
        return {"error": f"Tool error: {e}"}


def _tool_summary(name: str, args: dict, result: dict) -> str:
    if result.get("error"):
        return f"{name}: error — {result['error']}"
    if name == "run_sql":
        return f"run_sql ({result.get('row_count', 0)} rows): {args.get('query','')[:160]}"
    if name == "read_file":
        return f"read_file {args.get('path','')} ({result.get('lines','?')} lines)"
    if name == "search_code":
        return f"search_code /{args.get('pattern','')}/ ({len(result.get('hits', []))} hits)"
    if name == "read_doc":
        return f"read_doc {args.get('name','(list)')}"
    if name == "read_schema":
        return f"read_schema {args.get('table','(tables)')}"
    if name == "file_request":
        return f"file_request [{args.get('type','?')}] {args.get('title','')[:120]} → queued for approval"
    return name


# ── Streaming chat (manual tool-use loop) ─────────────────────────────────────

def stream_chat(history, user_message, actor=None, session_id=None):
    """
    Generator yielding event dicts for SSE:
      {"type":"text","text": "..."}            incremental answer text
      {"type":"tool","summary": "..."}          a tool call was made
      {"type":"done","content": "...","tool_trace":[...]}
      {"type":"error","error":"..."}
    `history` is a list of {"role","content"} (text only) prior turns.
    `actor` / `session_id` attribute any filed requests to the user + chat thread.
    """
    ctx = {"actor": actor, "session_id": session_id}
    if not is_enabled():
        yield {"type": "error", "error": "The agent is not configured. Set ANTHROPIC_API_KEY in backend/.env and restart."}
        return

    import anthropic

    client = anthropic.Anthropic(api_key=_api_key())
    model = os.getenv("PORTAL_AGENT_MODEL", "claude-opus-4-8")
    effort = os.getenv("PORTAL_AGENT_EFFORT", "high")
    system = _build_system()

    messages = []
    for h in (history or []):
        role = h.get("role")
        content = h.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    answer_parts = []
    tool_trace = []
    MAX_TURNS = 8

    try:
        for _turn in range(MAX_TURNS):
            with client.messages.stream(
                model=model,
                max_tokens=16000,
                system=system,
                tools=TOOLS,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                messages=messages,
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                        chunk = event.delta.text
                        answer_parts.append(chunk)
                        yield {"type": "text", "text": chunk}
                final = stream.get_final_message()

            if final.stop_reason != "tool_use":
                break

            # Append assistant turn (preserves thinking + tool_use blocks), run tools.
            messages.append({"role": "assistant", "content": final.content})
            tool_results = []
            for block in final.content:
                if getattr(block, "type", "") == "tool_use":
                    args = block.input if isinstance(block.input, dict) else {}
                    result = _execute_tool(block.name, args, ctx)
                    summary = _tool_summary(block.name, args, result)
                    tool_trace.append(summary)
                    yield {"type": "tool", "summary": summary}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str)[:60000],
                        "is_error": bool(result.get("error")),
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            yield {"type": "text", "text": "\n\n_(stopped after reaching the tool-call limit)_"}

    except Exception as e:
        logger.exception("portal agent stream failed")
        yield {"type": "error", "error": f"Agent error: {e}"}
        return

    yield {"type": "done", "content": "".join(answer_parts), "tool_trace": tool_trace}
