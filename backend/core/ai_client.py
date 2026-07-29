"""
core/ai_client.py — one minimal Anthropic tool-call helper shared by the AI recon
engines (ai_rules, ai_anomalies, ai_insights). (ai_matcher predates this and keeps
its own copy; new engines share this so model resolution + the forced-tool-call
shape stay identical.)

It does NOT de-identify — every caller must run its data through core.ai_deident
BEFORE building the `user` message. This helper only forces a single tool call and
returns the validated input dict.

`portal_agent` is imported at module top on purpose: tests monkeypatch
`ai_client.portal_agent` (or `is_enabled`) to stay fully offline. An in-function
re-import would shadow that and could fire a real API call.
"""
import os

from core import portal_agent


def is_enabled() -> bool:
    """True when a key + the SDK are available (delegates to portal_agent)."""
    return portal_agent.is_enabled()


def model_name() -> str:
    return os.getenv("PORTAL_AGENT_MODEL", "claude-opus-4-8")


def call_tool(system: str, user: str, tool: dict, *, max_tokens: int = 4000) -> dict:
    """
    Force exactly one call to `tool` and return its input dict (or {} if the model
    somehow returns no tool_use block). Raises on any transport/SDK error — callers
    catch and degrade gracefully so the UI never sees a 500.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=portal_agent._api_key())
    resp = client.messages.create(
        model=model_name(),
        max_tokens=max_tokens,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == tool["name"]:
            return block.input if isinstance(block.input, dict) else {}
    return {}
