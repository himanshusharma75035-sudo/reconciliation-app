"""
core.ai_client — provider-agnostic tool-call helper. Verifies config resolution (backward-compat
Anthropic default + OpenAI/self-hosted), the enabled gate, and the OpenAI-compatible request
shape + response parsing (tool_calls and the JSON-in-content fallback). Fully offline: httpx.post
is monkeypatched, so no network and no real key.
"""
import json
import httpx
import core.ai_client as ai_client

TOOL = {"name": "propose", "description": "d",
        "input_schema": {"type": "object", "properties": {"pairs": {"type": "array"}}}}


def _settings(monkeypatch, mapping):
    monkeypatch.setattr(ai_client, "_setting", lambda k: mapping.get(k, ""))


def _resp(monkeypatch, payload, capture=None):
    class R:
        def raise_for_status(self): pass
        def json(self): return payload
    def fake_post(url, json=None, headers=None, timeout=None):
        if capture is not None:
            capture.update(url=url, body=json, headers=headers)
        return R()
    monkeypatch.setattr(httpx, "post", fake_post)


# ── config resolution ─────────────────────────────────────────────────────────
def test_defaults_to_anthropic_with_legacy_key(monkeypatch):
    _settings(monkeypatch, {})
    monkeypatch.setattr(ai_client.portal_agent, "_api_key", lambda: "sk-ant-legacy", raising=False)
    cfg = ai_client.ai_config()
    assert cfg["provider"] == "anthropic"
    assert cfg["api_key"] == "sk-ant-legacy"          # falls back to the existing Anthropic key
    assert cfg["model"] == "claude-opus-4-8"


def test_openai_provider_uses_default_base(monkeypatch):
    _settings(monkeypatch, {"ai_provider": "openai", "ai_api_key": "sk-x", "ai_model": "gpt-4o"})
    cfg = ai_client.ai_config()
    assert cfg == {"provider": "openai", "api_key": "sk-x",
                   "base_url": "https://api.openai.com/v1", "model": "gpt-4o"}


def test_selfhosted_keyless_endpoint_is_enabled(monkeypatch):
    _settings(monkeypatch, {"ai_provider": "openai", "ai_base_url": "http://localhost:11434/v1",
                            "ai_model": "llama3.1"})
    assert ai_client.ai_config()["base_url"] == "http://localhost:11434/v1"
    assert ai_client.is_enabled() is True             # a self-hosted endpoint needs no key


def test_openai_needs_key_or_endpoint(monkeypatch):
    _settings(monkeypatch, {"ai_provider": "openai"})  # no key, default base
    assert ai_client.is_enabled() is False


# ── OpenAI-compatible transport ───────────────────────────────────────────────
def test_call_openai_builds_request_and_parses_tool_call(monkeypatch):
    _settings(monkeypatch, {"ai_provider": "openai", "ai_api_key": "sk-x", "ai_model": "m"})
    cap = {}
    _resp(monkeypatch, {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "propose", "arguments": json.dumps({"pairs": [{"b": "B1", "i": "I1"}]})}}]}}]}, cap)
    out = ai_client.call_tool("sys", "usr", TOOL)
    assert out == {"pairs": [{"b": "B1", "i": "I1"}]}
    assert cap["url"].endswith("/chat/completions")
    assert cap["headers"]["Authorization"] == "Bearer sk-x"
    fn = cap["body"]["tools"][0]["function"]
    assert fn["name"] == "propose" and fn["parameters"] == TOOL["input_schema"]
    assert cap["body"]["tool_choice"]["function"]["name"] == "propose"
    assert cap["body"]["model"] == "m"


def test_call_openai_json_in_content_fallback(monkeypatch):
    _settings(monkeypatch, {"ai_provider": "openai", "ai_api_key": "sk-x", "ai_model": "m"})
    _resp(monkeypatch, {"choices": [{"message": {"content": 'ok here: {"pairs": []} thanks'}}]})
    assert ai_client.call_tool("s", "u", TOOL) == {"pairs": []}


def test_call_openai_no_auth_header_when_keyless(monkeypatch):
    _settings(monkeypatch, {"ai_provider": "openai", "ai_base_url": "http://local/v1", "ai_model": "m"})
    cap = {}
    _resp(monkeypatch, {"choices": [{"message": {"tool_calls": []}}]}, cap)
    ai_client.call_tool("s", "u", TOOL)
    assert "Authorization" not in cap["headers"]
    assert cap["url"] == "http://local/v1/chat/completions"
