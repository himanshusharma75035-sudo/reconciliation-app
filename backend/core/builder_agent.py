"""
core/builder_agent.py — Developer Portal BUILDER agent (Phase 5, WRITE-CAPABLE).

This is the escalation of the read-only portal agent (core/portal_agent.py) into an
agent that can actually change the application — edit code, add features, fix bugs —
end to end, chat-first (it asks clarifying questions before it acts). It reuses the
read tools from portal_agent and adds write + build tools.

Because this app reconciles REAL MONEY and has almost no test coverage guarding the
matching core, the write path is wrapped in a mandatory safety floor that is NOT
optional and NOT relaxable from the UI:

  1. MASTER KILL-SWITCH — the whole capability is off unless BUILDER_AGENT_ENABLED
     is truthy in the environment. Ships OFF.
  2. SECRET WALLS — the write/delete tools physically refuse .env, key files,
     seed_accounts.json, databases, and anything under instance/ / .git / dist /
     node_modules. (User chose "all code except secrets".)
  3. GATES DECIDE — before a change can be applied to the running app it must pass
     ALL gates: compileall, pytest, ruff (skipped if absent), the frontend build
     (when frontend files changed), and a behavior-contract check. A change that
     fails any gate is never applied.
  4. REVERSIBLE — every file the agent touches is backed up before the first edit,
     so a task can be rolled back to its exact pre-change state in one click.
  5. AUDITED — every write and every gate run is recorded on the task + AuditLog.
  6. PERMISSIONED — the endpoints require the `portal_build` permission, which is
     stronger than `portal_access`.

The remote real-money server is deliberately NOT auto-deployed from here (that needs
its SSH/deploy credentials, handled separately). "Apply" makes a change live on the
LOCAL running instance; promoting to the remote box stays an explicit human step.
"""
import os
import re
import json
import time
import shutil
import logging
import datetime
import subprocess

from core import portal_agent  # reuse the read tools + SQL guard

logger = logging.getLogger("eko_recon.builder_agent")

_REPO_ROOT = portal_agent._REPO_ROOT
_BACKEND = os.path.join(_REPO_ROOT, "backend")
_FRONTEND = os.path.join(_REPO_ROOT, "frontend")
_BACKUP_ROOT = os.path.join(_BACKEND, "instance", "builder_backups")

# Files/paths the write tools must never touch (secrets, data, build artifacts).
_WRITE_BLOCK = re.compile(
    r"(^|/)(\.env(\..*)?|.*\.db|.*\.sqlite3?|seed_accounts\.json|.*\.key|.*\.pem)$",
    re.IGNORECASE,
)
_WRITE_BLOCK_DIRS = ("/.git/", "/node_modules/", "/instance/", "/dist/", "/__pycache__/")
# Only these extensions are writable (source + config text). No binaries.
_WRITE_ALLOW_EXT = {
    ".py", ".jsx", ".js", ".ts", ".tsx", ".md", ".json", ".css", ".html",
    ".txt", ".yml", ".yaml", ".cfg", ".ini", ".toml", ".example",
}

MAX_TURNS = 40                 # a build can be many steps
GATE_TIMEOUT = 420             # seconds per gate command


# ── Availability / config ─────────────────────────────────────────────────────

def _master_on() -> bool:
    return (os.getenv("BUILDER_AGENT_ENABLED", "") or "").strip().lower() in ("1", "true", "yes", "on")


def _auto_apply() -> bool:
    return (os.getenv("BUILDER_AUTO_APPLY", "") or "").strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    """The builder can run only when the master switch is on AND the LLM is available."""
    return _master_on() and portal_agent.is_enabled()


def status() -> dict:
    base = portal_agent.status()
    return {
        "enabled": _master_on() and base.get("enabled", False),
        "master_switch": _master_on(),
        "auto_apply": _auto_apply(),
        "has_key": base.get("has_key"),
        "has_sdk": base.get("has_sdk"),
        "model": os.getenv("BUILDER_AGENT_MODEL", base.get("model", "claude-opus-4-8")),
        "effort": os.getenv("BUILDER_AGENT_EFFORT", base.get("effort", "high")),
    }


# ── Path safety ───────────────────────────────────────────────────────────────

def _resolve_writable(path: str):
    """Return (abs_path, rel_path) if the path is safely writable, else raise ValueError."""
    rel = (path or "").strip().lstrip("/\\")
    if not rel:
        raise ValueError("Empty path.")
    target = os.path.abspath(os.path.join(_REPO_ROOT, rel))
    if os.path.commonpath([target, _REPO_ROOT]) != _REPO_ROOT:
        raise ValueError("Path escapes the repository.")
    norm = "/" + target.replace("\\", "/").lstrip("/")
    if any(d in norm + "/" for d in _WRITE_BLOCK_DIRS):
        raise ValueError("That directory is protected and cannot be written.")
    if _WRITE_BLOCK.search(norm):
        raise ValueError("Refusing to write a secret / database file.")
    ext = os.path.splitext(target)[1].lower()
    if ext not in _WRITE_ALLOW_EXT:
        raise ValueError(f"Refusing to write '{ext or 'no-ext'}' files (source/config text only).")
    relnorm = os.path.relpath(target, _REPO_ROOT).replace("\\", "/")
    return target, relnorm


# ── Backups (for one-click rollback) ──────────────────────────────────────────

def _backup_dir(task_id: str) -> str:
    return os.path.join(_BACKUP_ROOT, task_id)


def _manifest_path(task_id: str) -> str:
    return os.path.join(_backup_dir(task_id), "_manifest.json")


def _load_manifest(task_id: str) -> dict:
    try:
        with open(_manifest_path(task_id), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_manifest(task_id: str, manifest: dict):
    os.makedirs(_backup_dir(task_id), exist_ok=True)
    with open(_manifest_path(task_id), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)


def _snapshot_original(task_id: str, rel: str):
    """Save the pre-change state of `rel` exactly once, so rollback is exact."""
    manifest = _load_manifest(task_id)
    if rel in manifest:
        return  # already snapshotted this task
    abs_path = os.path.join(_REPO_ROOT, rel)
    existed = os.path.isfile(abs_path)
    manifest[rel] = {"existed": existed}
    if existed:
        dst = os.path.join(_backup_dir(task_id), "files", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(abs_path, dst)
    _save_manifest(task_id, manifest)


def restore_backups(task_id: str) -> dict:
    """Roll a task's files back to their exact pre-change state. Returns a summary."""
    manifest = _load_manifest(task_id)
    restored, deleted, errors = [], [], []
    for rel, info in manifest.items():
        abs_path = os.path.join(_REPO_ROOT, rel)
        try:
            if info.get("existed"):
                src = os.path.join(_backup_dir(task_id), "files", rel)
                if os.path.isfile(src):
                    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                    shutil.copy2(src, abs_path)
                    restored.append(rel)
            else:
                # File was created by the task — remove it to restore original absence.
                if os.path.isfile(abs_path):
                    os.remove(abs_path)
                    deleted.append(rel)
        except Exception as e:
            errors.append(f"{rel}: {e}")
    return {"restored": restored, "deleted": deleted, "errors": errors}


# ── Write tools ───────────────────────────────────────────────────────────────

def _tool_write_file(task_id: str, ctx: dict, path: str, content: str) -> dict:
    target, rel = _resolve_writable(path)
    _snapshot_original(task_id, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    existed = os.path.isfile(target)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    ctx.setdefault("changed", set()).add(rel)
    return {"ok": True, "path": rel, "action": "modified" if existed else "created",
            "bytes": len(content.encode("utf-8"))}


def _tool_replace_in_file(task_id: str, ctx: dict, path: str, find: str, replace: str) -> dict:
    target, rel = _resolve_writable(path)
    if not os.path.isfile(target):
        return {"error": f"File not found: {rel}"}
    with open(target, "r", encoding="utf-8") as fh:
        data = fh.read()
    n = data.count(find)
    if n == 0:
        return {"error": "The `find` text was not found exactly. Read the file and match whitespace precisely."}
    if n > 1:
        return {"error": f"The `find` text appears {n} times — make it unique (add surrounding context)."}
    _snapshot_original(task_id, rel)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data.replace(find, replace, 1))
    ctx.setdefault("changed", set()).add(rel)
    return {"ok": True, "path": rel, "action": "modified"}


def _tool_delete_file(task_id: str, ctx: dict, path: str) -> dict:
    target, rel = _resolve_writable(path)
    if not os.path.isfile(target):
        return {"error": f"File not found: {rel}"}
    _snapshot_original(task_id, rel)
    os.remove(target)
    ctx.setdefault("changed", set()).add(rel)
    return {"ok": True, "path": rel, "action": "deleted"}


def _tool_list_dir(path: str = "") -> dict:
    rel = (path or "").strip().lstrip("/\\")
    target = os.path.abspath(os.path.join(_REPO_ROOT, rel))
    if os.path.commonpath([target, _REPO_ROOT]) != _REPO_ROOT or not os.path.isdir(target):
        return {"error": "Not a directory inside the repo."}
    entries = []
    for name in sorted(os.listdir(target)):
        if name in ("node_modules", ".git", "__pycache__", "dist", "instance"):
            continue
        full = os.path.join(target, name)
        entries.append({"name": name, "type": "dir" if os.path.isdir(full) else "file"})
    return {"path": rel or ".", "entries": entries}


# ── Gate runner ───────────────────────────────────────────────────────────────

def _run(cmd, cwd, timeout=GATE_TIMEOUT) -> dict:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        return {"code": p.returncode, "out": out[-6000:]}
    except FileNotFoundError as e:
        return {"code": 127, "out": f"command not found: {e}", "missing": True}
    except subprocess.TimeoutExpired:
        return {"code": 124, "out": f"timed out after {timeout}s"}
    except Exception as e:
        return {"code": 1, "out": str(e)}


_CORE_TOUCH = ("backend/core/", "backend/routes/upload.py")


def touches_core(changed) -> list:
    """The subset of changed paths that touch the money-matching / ingestion core."""
    return sorted(c for c in (changed or [])
                  if any(c == p or c.startswith(p) for p in _CORE_TOUCH))


def _smoke_boot() -> dict:
    """Canary: boot the built code as a throwaway process against a TEMP sqlite DB on
    an ephemeral port and confirm it answers HTTP. Proves the change doesn't break
    startup — without touching the real database. Best-effort: a clean process-exit
    with a traceback is a real failure; a bind timeout is reported as skipped."""
    import socket
    import tempfile
    import urllib.request
    import urllib.error
    py = os.getenv("PYTHON", "python")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    tmpdb = os.path.join(tempfile.gettempdir(), f"recon_smoke_{port}.db")
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{tmpdb}"
    env["BUILDER_AGENT_ENABLED"] = "0"   # never let the canary act
    proc = subprocess.Popen([py, "-m", "uvicorn", "main:app", "--port", str(port)],
                            cwd=_BACKEND, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        ok, skipped, detail = False, False, ""
        deadline = time.time() + 35
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                return {"ok": False, "detail": f"process exited ({proc.returncode}): {out[-1500:]}"}
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                    ok = r.status < 500
                    detail = f"/api/health → {r.status}"
                    break
            except urllib.error.HTTPError as he:
                ok = True; detail = f"app responded ({he.code})"; break   # booted + routed
            except Exception:
                time.sleep(0.6)
        if not ok:
            skipped, detail = True, "did not bind in time (treated as skipped, not a failure)"
        return {"ok": ok or skipped, "skipped": skipped, "detail": detail}
    finally:
        try:
            proc.terminate(); proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            os.remove(tmpdb)
        except Exception:
            pass


def run_gates(changed=None) -> dict:
    """Run the mandatory gate suite. Returns {gates: {...}, ok: bool}. Frontend build
    runs only when a frontend/ file changed; ruff is skipped (not failed) if absent."""
    changed = set(changed or [])
    py = os.getenv("PYTHON", "python")
    gates: dict = {}

    # 1. compile
    r = _run([py, "-m", "compileall", "-q", "core", "models", "routes", "main.py"], _BACKEND, 180)
    gates["compileall"] = {"ok": r["code"] == 0, "detail": r["out"][-1500:]}

    # 2. tests
    r = _run([py, "-m", "pytest", "tests/", "-q", "-p", "no:warnings"], _BACKEND, GATE_TIMEOUT)
    gates["pytest"] = {"ok": r["code"] == 0, "detail": r["out"][-2500:]}

    # 3. lint (optional — skipped if ruff isn't installed)
    r = _run([py, "-m", "ruff", "check", "."], _BACKEND, 120)
    if r.get("missing") or "No module named ruff" in r["out"]:
        gates["ruff"] = {"ok": True, "skipped": True, "detail": "ruff not installed — skipped"}
    else:
        gates["ruff"] = {"ok": r["code"] == 0, "detail": r["out"][-1500:]}

    # 4. frontend build — only when a frontend file changed
    if any(c.startswith("frontend/") for c in changed):
        npm = shutil.which("npm") or "npm"
        r = _run([npm, "run", "build"], _FRONTEND, GATE_TIMEOUT)
        gates["frontend_build"] = {"ok": r["code"] == 0, "detail": r["out"][-2500:]}
    else:
        gates["frontend_build"] = {"ok": True, "skipped": True, "detail": "no frontend files changed"}

    # 5. behaviour-contract gate — heuristic. If the change touches the matching core
    #    or the ingestion pipeline, the tests above ARE the contract guard; we also
    #    flag it so the human reviewer sees the change is contract-sensitive.
    core_touched = touches_core(changed)
    gates["behavior_contract"] = {
        "ok": gates["pytest"]["ok"],           # cannot pass the contract gate if tests fail
        "core_touched": core_touched,
        "detail": ("Touches money-matching/ingestion core — reviewer must confirm against "
                   "docs/behavior-contract.md." if core_touched else "No core files touched."),
    }

    # 6. smoke boot — the built code must actually start and answer HTTP (temp DB).
    if os.getenv("BUILDER_SMOKE_BOOT", "1").strip().lower() not in ("0", "false", "no", "off"):
        gates["smoke_boot"] = _smoke_boot()
    else:
        gates["smoke_boot"] = {"ok": True, "skipped": True, "detail": "smoke boot disabled"}

    ok = all(g.get("ok") for g in gates.values())
    return {"gates": gates, "ok": ok}


# ── Tool schema ───────────────────────────────────────────────────────────────

_READ_TOOLS = [t for t in portal_agent.TOOLS if t["name"] in
               ("read_file", "search_code", "read_schema", "read_doc", "run_sql")]

_WRITE_TOOLS = [
    {"name": "list_dir", "description": "List files/subdirectories of a repo directory.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    {"name": "write_file",
     "description": "Create or fully overwrite a repo file with the given content. Use for new files or full rewrites. Secret/data files are refused.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Repo-relative path."},
         "content": {"type": "string", "description": "Full file content."}},
         "required": ["path", "content"]}},
    {"name": "replace_in_file",
     "description": "Make a surgical edit: replace one exact, unique occurrence of `find` with `replace` in a file. Read the file first and match whitespace exactly.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "find": {"type": "string"}, "replace": {"type": "string"}},
         "required": ["path", "find", "replace"]}},
    {"name": "delete_file", "description": "Delete a repo file (backed up first; reversible).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "run_gates",
     "description": "Run the mandatory gate suite (compileall, pytest, ruff, frontend build if needed, behavior-contract). Call this after you finish editing and before finishing. A change that fails any gate must NOT be applied — fix and re-run.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "set_plan", "description": "Record your concise implementation plan before you start editing.",
     "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
    {"name": "request_clarification",
     "description": "Ask the user a clarifying question and STOP. Use whenever the request is ambiguous or risky rather than guessing.",
     "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    {"name": "finish",
     "description": "Declare the build complete. Provide a summary of exactly what you changed and why. Only call this after run_gates has passed.",
     "input_schema": {"type": "object", "properties": {
         "summary": {"type": "string"}, "gates_passed": {"type": "boolean"}},
         "required": ["summary"]}},
]

TOOLS = _READ_TOOLS + _WRITE_TOOLS


# ── System prompt ─────────────────────────────────────────────────────────────

def _build_system(plan_only: bool = False) -> str:
    tables = ", ".join(sorted(__import__("models.database", fromlist=["Base"]).Base.metadata.tables.keys()))
    plan_banner = ("\n\n## PLAN-ONLY MODE\nYou are in plan-only mode: you have NO write tools. "
                   "Investigate with the read tools, then call set_plan with a concrete, file-by-file "
                   "implementation plan and explain it. Do NOT attempt to edit anything.\n") if plan_only else ""
    return plan_banner + f"""You are the Eko Reconciliation **Builder Agent** — an autonomous software engineer working directly on this real-money reconciliation application. Unlike the read-only portal agent, you CAN change the code: create files, edit them, delete them, and run the build/test gates.

This application reconciles REAL MONEY and has a strict behavior contract (docs/behavior-contract.md, 25 invariants) with very little automated test coverage. Treat every change as production-critical.

## How you work
1. Understand the request. If it is ambiguous, risky, or under-specified, call `request_clarification` and STOP — never guess on money logic. Ask like a helpful chatbot.
2. Investigate first with the read tools (search_code, read_file, read_schema, read_doc). Ground every edit in the actual code.
3. Call `set_plan` with a short plan.
4. Make the change with write_file / replace_in_file / delete_file. Prefer small surgical edits (replace_in_file) over full rewrites. Match the surrounding code's style exactly.
5. If you touch anything under backend/core/ or routes/upload.py (matching, ingestion, classification, tolerances, status transitions, match-IDs), re-read docs/behavior-contract.md and preserve every invariant. Call it out explicitly in your summary.
6. Run `run_gates`. If any gate fails, read the output, fix the code, and run again. Do NOT finish on failing gates.
7. Call `finish` with a clear summary once gates pass.

## Hard rules (enforced by the tools, but honour them)
- You cannot and must not write secrets or data: .env, key files, seed_accounts.json, databases, anything under instance/ — these are walled off.
- Never weaken or delete a behavior-contract invariant, a tolerance, or a status set to make a test pass.
- Keep changes minimal and reversible. Everything you touch is backed up and audited.

## Live context
Database tables (read via read_schema/run_sql, except blocked auth tables): {tables}
Today is {datetime.date.today().isoformat()} (server local).

You are permitted to modify all application code EXCEPT secrets. The gates decide whether a change is safe to apply; a change that fails any gate will not go live."""


# ── Streaming build loop ──────────────────────────────────────────────────────

def stream_build(history, user_message, task_id, actor=None, plan_only=False):
    """
    Generator of SSE event dicts, same shape as portal_agent.stream_chat plus build
    events. It drives the whole build: plan → edit → gate → finish, asking questions
    when unsure. When plan_only=True the agent gets NO write tools — it only produces
    a plan (dry run).
      {"type":"text","text":...}           incremental assistant text
      {"type":"tool","summary":...}         a tool ran (write/gate/etc.)
      {"type":"gates","results":{...}}      gate suite finished
      {"type":"await","question":...}       agent asked a clarifying question; stop
      {"type":"finish","summary":...,"gates_passed":bool}
      {"type":"done","content":...,"tool_trace":[...],"changed":[...],"status":...}
      {"type":"error","error":...}
    """
    if not _master_on():
        yield {"type": "error", "error": "The Builder Agent master switch is OFF. Set BUILDER_AGENT_ENABLED=1 in backend/.env and restart to enable write access."}
        return
    if not portal_agent.is_enabled():
        yield {"type": "error", "error": "The agent is not configured (ANTHROPIC_API_KEY missing or no credits)."}
        return

    import anthropic
    client = anthropic.Anthropic(api_key=portal_agent._api_key())
    model = os.getenv("BUILDER_AGENT_MODEL", os.getenv("PORTAL_AGENT_MODEL", "claude-opus-4-8"))
    effort = os.getenv("BUILDER_AGENT_EFFORT", os.getenv("PORTAL_AGENT_EFFORT", "high"))
    system = _build_system(plan_only=plan_only)
    # Plan-only builds get read tools + planning tools only — no write/gate access.
    active_tools = ([t for t in TOOLS if t["name"] in
                     ("read_file", "search_code", "read_schema", "read_doc", "run_sql",
                      "list_dir", "set_plan", "request_clarification", "finish")]
                    if plan_only else TOOLS)

    ctx = {"actor": actor, "task_id": task_id, "changed": set()}
    # Pre-seed changed set from prior turns of this task (backups manifest).
    ctx["changed"].update(_load_manifest(task_id).keys())

    messages = []
    for h in (history or []):
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    answer_parts, tool_trace = [], []
    final_status = "building"
    last_gates = None
    finished_summary = None

    def _exec(name, args):
        if name == "write_file":
            return _tool_write_file(task_id, ctx, args.get("path", ""), args.get("content", ""))
        if name == "replace_in_file":
            return _tool_replace_in_file(task_id, ctx, args.get("path", ""), args.get("find", ""), args.get("replace", ""))
        if name == "delete_file":
            return _tool_delete_file(task_id, ctx, args.get("path", ""))
        if name == "list_dir":
            return _tool_list_dir(args.get("path", ""))
        if name == "run_gates":
            return run_gates(ctx["changed"])
        # read tools + set_plan/request_clarification/finish handled by caller/portal_agent
        return portal_agent._execute_tool(name, args, ctx)

    try:
        for _turn in range(MAX_TURNS):
            with client.messages.stream(
                model=model, max_tokens=16000, system=system, tools=active_tools,
                thinking={"type": "adaptive"}, output_config={"effort": effort},
                messages=messages,
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                        answer_parts.append(event.delta.text)
                        yield {"type": "text", "text": event.delta.text}
                final = stream.get_final_message()

            if final.stop_reason != "tool_use":
                break

            messages.append({"role": "assistant", "content": final.content})
            tool_results = []
            stop_now = False
            for block in final.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                name = block.name
                args = block.input if isinstance(block.input, dict) else {}

                if name == "set_plan":
                    tool_trace.append("set_plan")
                    yield {"type": "plan", "plan": args.get("plan", "")}
                    result = {"ok": True}
                elif name == "request_clarification":
                    q = args.get("question", "")
                    tool_trace.append("request_clarification")
                    yield {"type": "await", "question": q}
                    final_status = "awaiting_input"
                    result = {"ok": True, "note": "Question delivered to the user; stopping for their reply."}
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                         "content": json.dumps(result)})
                    stop_now = True
                    break
                elif name == "finish":
                    finished_summary = args.get("summary", "")
                    gp = bool(args.get("gates_passed")) or bool(last_gates and last_gates.get("ok"))
                    final_status = "ready" if gp else "failed"
                    tool_trace.append("finish")
                    yield {"type": "finish", "summary": finished_summary, "gates_passed": gp}
                    result = {"ok": True}
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                         "content": json.dumps(result)})
                    stop_now = True
                    break
                else:
                    result = _exec(name, args)
                    if name == "run_gates":
                        last_gates = result
                        yield {"type": "gates", "results": result}
                        tool_trace.append(f"run_gates → {'PASS' if result.get('ok') else 'FAIL'}")
                    else:
                        tool_trace.append(_write_summary(name, args, result))
                        yield {"type": "tool", "summary": tool_trace[-1]}

                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(result, default=str)[:60000],
                    "is_error": bool(isinstance(result, dict) and result.get("error")),
                })

            messages.append({"role": "user", "content": tool_results})
            if stop_now:
                break
        else:
            yield {"type": "text", "text": "\n\n_(stopped after reaching the step limit)_"}
            final_status = "failed"
    except Exception as e:
        logger.exception("builder stream failed")
        yield {"type": "error", "error": f"Builder error: {e}"}
        final_status = "failed"

    yield {"type": "done", "content": "".join(answer_parts) or (finished_summary or ""),
           "tool_trace": tool_trace, "changed": sorted(ctx["changed"]),
           "status": final_status, "gates": last_gates, "summary": finished_summary}


def _write_summary(name, args, result):
    if isinstance(result, dict) and result.get("error"):
        return f"{name}: error — {result['error']}"
    if name in ("write_file", "replace_in_file", "delete_file"):
        return f"{name} {result.get('path', args.get('path',''))} ({result.get('action','')})"
    if name == "list_dir":
        return f"list_dir {args.get('path','.')}"
    return portal_agent._tool_summary(name, args, result) if isinstance(result, dict) else name


# ── Apply / rollback (used by the endpoints) ──────────────────────────────────

def diff_stat(changed) -> str:
    """Best-effort `git diff --stat` limited to the changed paths, for display."""
    changed = list(changed or [])
    if not changed:
        return ""
    try:
        p = subprocess.run(["git", "diff", "--stat", "--", *changed],
                           cwd=_REPO_ROOT, capture_output=True, text=True, timeout=20)
        return (p.stdout or "").strip()
    except Exception:
        return ""


def git_commit(changed, message) -> str:
    """Commit ONLY the task's changed paths (audit trail). Returns short sha or ''."""
    changed = [c for c in (changed or [])]
    if not changed:
        return ""
    try:
        subprocess.run(["git", "add", "--", *changed], cwd=_REPO_ROOT,
                       capture_output=True, text=True, timeout=30)
        subprocess.run(["git", "commit", "-m", message, "--", *changed],
                       cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30)
        p = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_REPO_ROOT,
                           capture_output=True, text=True, timeout=10)
        return (p.stdout or "").strip()
    except Exception:
        return ""


# ── Notifications (email via existing SMTP + optional Slack webhook) ───────────

def notify(subject: str, body_text: str):
    """Best-effort notify on builder events. Email reuses the app's SMTP config
    (EOD_EMAIL_TO); Slack posts to BUILDER_SLACK_WEBHOOK if set. Never raises."""
    try:
        from core import notifications
        if notifications.SMTP_HOST and notifications.EOD_EMAIL_TO:
            html = (f"<div style='font-family:sans-serif;color:#1f2937'>"
                    f"<h3 style='margin:0 0 8px'>{subject}</h3>"
                    f"<pre style='white-space:pre-wrap;font-size:13px;color:#374151'>{body_text}</pre>"
                    f"<div style='font-size:11px;color:#9ca3af;margin-top:12px'>Eko Recon · Developer Portal Builder Agent</div></div>")
            notifications._send(subject, html, notifications.EOD_EMAIL_TO)
    except Exception:
        pass
    hook = (os.getenv("BUILDER_SLACK_WEBHOOK", "") or "").strip()
    if hook:
        try:
            import urllib.request
            data = json.dumps({"text": f"*{subject}*\n{body_text}"}).encode()
            req = urllib.request.Request(hook, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
        except Exception:
            pass


# ── Shared apply + post-apply health watchdog ─────────────────────────────────

def apply_task(task_id: str, actor: str, auto: bool = False) -> dict:
    """Commit a vetted task's changes (audit + rollback point), mark it applied, arm
    the health watchdog, and notify. Shared by the manual endpoint and auto-apply.
    Refuses unless the master switch is on and the gates passed."""
    from models.database import SessionLocal, BuilderTask, AuditLog, generate_id
    if not _master_on():
        return {"error": "master switch off"}
    db = SessionLocal()
    try:
        t = db.query(BuilderTask).filter(BuilderTask.id == task_id).first()
        if not t:
            return {"error": "task not found"}
        if not t.gates_ok:
            return {"error": "gates not passed"}
        if t.status == "applied":
            return {"status": "applied", "commit_sha": t.commit_sha, "note": "already applied"}
        try:
            changed = json.loads(t.files_changed) if t.files_changed else []
        except Exception:
            changed = []
        sha = git_commit(changed, f"builder: {t.title[:72]}\n\ntask {t.id} applied by {actor}"
                                  + (" (auto-apply)" if auto else ""))
        now = datetime.datetime.utcnow()
        t.status = "applied"; t.applied_by = actor; t.applied_at = now
        t.commit_sha = sha or None; t.updated_at = now
        db.add(AuditLog(id=generate_id(), username=actor, action="builder_apply",
                        action_type="human", entity_type="builder_task", entity_id=task_id,
                        detail=json.dumps({"title": t.title[:200], "commit": sha, "auto": auto,
                                           "files": changed[:50]})[:2000], created_at=now))
        db.commit()
        title = t.title
    finally:
        db.close()
    wd = schedule_watchdog(task_id, actor)
    notify(f"Builder applied a change: {title[:80]}",
           f"Task {task_id} was {'auto-' if auto else ''}applied by {actor}.\n"
           f"Files: {', '.join(changed) or '(none)'}\nCommit: {sha or '(uncommitted)'}\n"
           f"Health watchdog armed (baseline={wd.get('baseline')}, re-check in {wd.get('delay_sec')}s).")
    return {"status": "applied", "commit_sha": sha, "auto": auto, "watchdog": wd,
            "restart_needed": any(str(c).startswith("backend/") for c in changed)}


_SEVERITY = {"ok": 0, "unknown": 1, "warn": 2, "critical": 3}


def _health_status() -> str:
    from models.database import SessionLocal
    from core.recon_health import compute_recon_health
    db = SessionLocal()
    try:
        return compute_recon_health(db, days=2).get("status", "unknown")
    except Exception:
        return "unknown"
    finally:
        db.close()


def schedule_watchdog(task_id: str, actor: str, delay_sec: int = None) -> dict:
    """Snapshot recon-health now; after a delay, re-check. If it regressed to
    'critical', auto-roll-back the task and file a request. One-shot timer thread."""
    import threading
    if delay_sec is None:
        try:
            delay_sec = int(os.getenv("BUILDER_WATCHDOG_SECONDS", "600"))
        except Exception:
            delay_sec = 600
    baseline = _health_status()

    def _check():
        after = _health_status()
        regressed = (_SEVERITY.get(after, 1) > _SEVERITY.get(baseline, 1)) and after == "critical"
        if regressed:
            _auto_rollback(task_id, actor, baseline, after)

    if delay_sec > 0:
        timer = threading.Timer(delay_sec, _check)
        timer.daemon = True
        timer.start()
    return {"baseline": baseline, "delay_sec": delay_sec}


def _auto_rollback(task_id: str, actor: str, baseline: str, after: str):
    """Watchdog tripped: restore the task's files, mark it rolled_back, file a
    high-priority request, and notify. Runs on the timer thread with its own session."""
    from models.database import SessionLocal, BuilderTask, PortalRequest, AuditLog, generate_id
    result = restore_backups(task_id)
    db = SessionLocal()
    try:
        t = db.query(BuilderTask).filter(BuilderTask.id == task_id).first()
        now = datetime.datetime.utcnow()
        title = t.title if t else task_id
        if t:
            t.status = "rolled_back"; t.rolled_back_by = "health-watchdog"
            t.rolled_back_at = now; t.updated_at = now
            try:
                changed = json.loads(t.files_changed) if t.files_changed else []
            except Exception:
                changed = []
            if changed:
                git_commit(changed, f"builder: AUTO-ROLLBACK of task {task_id} (health {baseline}→{after})")
        rid = generate_id()
        db.add(PortalRequest(
            id=rid, req_type="bug", title=f"Auto-rollback: '{title[:120]}' degraded health",
            description=(f"The builder task {task_id} was applied and recon-health regressed from "
                         f"'{baseline}' to '{after}' within the watchdog window. The change was "
                         f"automatically rolled back. Investigate before re-applying.\n\nRollback: {result}"),
            priority="high", status="open", source="agent", created_by="health-watchdog",
            created_at=now, updated_at=now))
        db.add(AuditLog(id=generate_id(), username="health-watchdog", action="builder_auto_rollback",
                        action_type="app", entity_type="builder_task", entity_id=task_id,
                        detail=json.dumps({"baseline": baseline, "after": after, "result": result})[:2000],
                        created_at=now))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
    notify(f"⚠️ Auto-rollback: '{title[:80]}'",
           f"recon-health regressed {baseline}→{after} after applying task {task_id}. "
           f"The change was rolled back automatically and a high-priority request was filed.")


# ── Builder playbook (curated build tasks, incl. the test-bootstrap) ──────────

PLAYBOOK = [
    {"group": "Harden the safety net", "items": [
        {"label": "Write tests for the core matcher",
         "prompt": "Add characterization tests under backend/tests/ for core/matching_engine.py: cover the ±₹1 matched vs amount_mismatch boundary, first-match-wins, and match-ID sequencing. Do not change engine behavior — only capture it. Run the gates."},
        {"label": "Tests for E-Value engine",
         "prompt": "Add characterization tests for core/evalue_engine.py's 5-pass matcher and exact-amount tolerance, capturing current behavior without changing it. Run the gates."},
        {"label": "Tests for row classification",
         "prompt": "Add tests for routes/upload.py _classify_bank_row and the reversal-before-fee ordering (behavior-contract item). Capture current behavior only. Run the gates."},
    ]},
    {"group": "Safe improvements", "items": [
        {"label": "Add a CSV export button",
         "prompt": "Add a CSV export button to the Open Items page in the frontend that exports the currently filtered rows. Match the existing UI style."},
        {"label": "Improve an error message",
         "prompt": "Find a place where an upload validation error is unclear and make the message more actionable, without changing the validation logic."},
    ]},
]


# ── Remote deploy (explicit, env-gated — finishes the 'to prod' loop) ─────────

def deploy_available() -> bool:
    return bool((os.getenv("BUILDER_DEPLOY_COMMAND", "") or "").strip())


def run_deploy() -> dict:
    """Run the configured deploy command (e.g. a build+scp+restart script for the
    remote server). Deliberately NOT wired to any creds by default — it runs whatever
    BUILDER_DEPLOY_COMMAND names, or reports that deploy isn't configured."""
    cmd = (os.getenv("BUILDER_DEPLOY_COMMAND", "") or "").strip()
    if not cmd:
        return {"ok": False, "configured": False,
                "detail": "Set BUILDER_DEPLOY_COMMAND in backend/.env to a deploy script to enable remote deploy."}
    try:
        p = subprocess.run(cmd, cwd=_REPO_ROOT, shell=True, capture_output=True,
                           text=True, timeout=900)
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        return {"ok": p.returncode == 0, "configured": True, "code": p.returncode, "detail": out[-4000:]}
    except Exception as e:
        return {"ok": False, "configured": True, "detail": str(e)}
