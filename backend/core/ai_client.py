"""
core/ai_client.py — the shared, PROVIDER-AGNOSTIC tool-call helper for the AI recon engines
(ai_rules, ai_anomalies, ai_insights, ai_matcher).

It supports two provider families which, between them, reach almost every model — hosted or
open-source, cloud or self-hosted:
  • anthropic — Anthropic's native Messages API.
  • openai    — the OpenAI Chat-Completions API with a configurable base_url. That ONE setting
                reaches OpenAI, Azure OpenAI, OpenRouter, Groq, Together, DeepSeek, Mistral,
                Google Gemini (its OpenAI-compatible endpoint) and any self-hosted server that
                speaks the OpenAI API — Ollama, vLLM, LM Studio, LocalAI, text-generation-webui.

Provider / key / base_url / model are set by an admin in Configuration → AI (stored in
SystemSetting). Everything falls back to the legacy Anthropic key + env, so an existing install
keeps working with zero reconfiguration. The Developer-Portal / Builder agent is unchanged — it
stays on Anthropic via core.portal_agent.

It does NOT de-identify — every caller must run its data through core.ai_deident BEFORE building
the `user` message. This helper only forces a single tool call and returns the validated input.

Tests monkeypatch each engine's `ai_client` reference (a SimpleNamespace) to stay fully offline.
"""
import json
import os

from core import portal_agent

_MODEL_DEFAULTS = {"anthropic": "claude-opus-4-8", "openai": "gpt-4o-mini"}
_OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"


def _setting(key: str) -> str:
    """Read one SystemSetting value ('' if absent / on any error)."""
    try:
        from models.database import SessionLocal, SystemSetting
        db = SessionLocal()
        try:
            row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
            return (row.value or "").strip() if row else ""
        finally:
            db.close()
    except Exception:
        return ""


def ai_config() -> dict:
    """Resolve the active recon-AI config → {provider, api_key, base_url, model}. Backward
    compatible: with nothing new configured it behaves exactly like the old Anthropic-only client
    (provider 'anthropic', the Config/env Anthropic key, the default Claude model)."""
    provider = (_setting("ai_provider") or os.getenv("AI_PROVIDER") or "anthropic").strip().lower()
    if provider not in ("anthropic", "openai"):
        provider = "anthropic"
    key = _setting("ai_api_key") or os.getenv("AI_API_KEY") or ""
    if not key and provider == "anthropic":
        key = portal_agent._api_key()            # legacy anthropic_api_key / ANTHROPIC_API_KEY
    base_url = (_setting("ai_base_url") or os.getenv("AI_BASE_URL") or "").rstrip("/")
    if provider == "openai" and not base_url:
        base_url = _OPENAI_DEFAULT_BASE
    model = (_setting("ai_model") or os.getenv("AI_MODEL")
             or (os.getenv("PORTAL_AGENT_MODEL") if provider == "anthropic" else "")
             or _MODEL_DEFAULTS[provider])
    return {"provider": provider, "api_key": (key or "").strip(), "base_url": base_url, "model": model.strip()}


def is_enabled() -> bool:
    """True when the active provider is usable: a key is configured (anthropic also needs its SDK),
    or an openai-compatible base_url points at a keyless self-hosted server (e.g. Ollama)."""
    cfg = ai_config()
    if cfg["provider"] == "anthropic":
        if not cfg["api_key"]:
            return False
        try:
            import anthropic  # noqa: F401
            return True
        except Exception:
            return False
    # openai-compatible: a key, or a non-default (self-hosted) endpoint, is enough
    return bool(cfg["api_key"] or (cfg["base_url"] and cfg["base_url"] != _OPENAI_DEFAULT_BASE))


def model_name() -> str:
    return ai_config()["model"]


def call_tool(system: str, user: str, tool: dict, *, max_tokens: int = 4000) -> dict:
    """Force exactly one call to `tool` and return its validated input dict (or {} if the model
    returned none). Raises on any transport/SDK error — callers catch and degrade gracefully so
    the UI never sees a 500."""
    cfg = ai_config()
    if cfg["provider"] == "anthropic":
        return _call_anthropic(cfg, system, user, tool, max_tokens)
    return _call_openai(cfg, system, user, tool, max_tokens)


def _call_anthropic(cfg: dict, system: str, user: str, tool: dict, max_tokens: int) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=cfg["api_key"])
    resp = client.messages.create(
        model=cfg["model"], max_tokens=max_tokens, system=system,
        tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == tool["name"]:
            return block.input if isinstance(block.input, dict) else {}
    return {}


def _call_openai(cfg: dict, system: str, user: str, tool: dict, max_tokens: int) -> dict:
    """OpenAI Chat-Completions with a forced function call — the shape every OpenAI-compatible
    server speaks. An Anthropic-style tool ({name, description, input_schema}) maps 1:1 onto an
    OpenAI function; a weak model that ignores the forced tool is caught by a JSON-in-content
    fallback so the caller still gets a dict."""
    import httpx
    fn = {"name": tool["name"], "description": tool.get("description", ""),
          "parameters": tool.get("input_schema") or tool.get("parameters") or {"type": "object"}}
    body = {
        "model": cfg["model"], "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "tools": [{"type": "function", "function": fn}],
        "tool_choice": {"type": "function", "function": {"name": tool["name"]}},
    }
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    r = httpx.post(f"{cfg['base_url']}/chat/completions", json=body, headers=headers, timeout=120)
    r.raise_for_status()
    data = r.json()
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    for c in (msg.get("tool_calls") or []):
        args = c.get("function", {}).get("arguments")
        if isinstance(args, dict):
            return args
        try:
            return json.loads(args or "{}")
        except Exception:
            return {}
    content = msg.get("content") or ""                        # fallback: JSON object in the text
    s, e = content.find("{"), content.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(content[s:e + 1])
        except Exception:
            return {}
    return {}
