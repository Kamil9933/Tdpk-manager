"""Gemini chat driver: tool calls with thought signatures, model fallback on
overload, and error text that never leaks the API key."""
import asyncio
import json
import os
import types

import httpx
import pytest

from core import agent
from core import gemini_fallback as gf


class DB:
    def __init__(self):
        self.rows = []
    async def add_agent_message(self, cid, role, content, **kw): self.rows.append({"role": role, "content": content})
    async def list_agent_messages(self, cid): return list(self.rows)
    async def get_automation_settings(self): return {}
    async def get_agent_conversation(self, cid): return {"title": "x"}
    async def bump_agent_conversation_tokens(self, *a, **k): pass


class Tool:
    def __init__(self, name):
        self.name, self.description = name, "d"
        self.inputSchema = {"type": "object", "properties": {"order": {"type": "string"}}}


class MCP:
    async def list_tools(self): return [Tool("get_order"), Tool("set_automation_setting")]
    async def call_tool(self, name, args):
        return [types.SimpleNamespace(text=json.dumps({"order": args.get("order"), "status": "paid"}))]


def text_resp(t):
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": t, "thoughtSignature": "TXT_SIG"}]}}]}


def call_resp(order, sig):
    return {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"name": "get_order", "args": {"order": order}}, "thoughtSignature": sig}]}}]}


@pytest.fixture
def gemini(monkeypatch):
    """Scripted Gemini: each request pops the next (status, body)."""
    state = {"script": [], "requests": [], "models": []}

    def handler(req):
        assert "key=" not in str(req.url), "API key must go in a header, never the URL"
        assert req.headers["x-goog-api-key"] == os.environ["GEMINI_API_KEY"]
        state["requests"].append(json.loads(req.content))
        state["models"].append(req.url.path.split("/models/")[1].split(":")[0])
        status, body = state["script"].pop(0)
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(gf.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(gf, "GEMINI_RETRY_PAUSE_SECONDS", 0)
    async def no_rule(*a, **k): return None
    monkeypatch.setattr(agent.rules, "try_handle", no_rule)
    return state


def turn(text, db=None):
    return asyncio.run(agent.run_agent_turn(MCP(), db or DB(), "c", text))


def test_plain_chat(gemini):
    gemini["script"] = [(200, text_resp("Hello Kamil"))]
    r = turn("hi, how's business?")
    assert r["text"] == "Hello Kamil" and r["provider"] == "gemini"


def test_tool_call_keeps_and_replays_thought_signature(gemini):
    gemini["script"] = [(200, call_resp("1234", "SIG_ABC")), (200, text_resp("Order 1234 is paid."))]
    db = DB()
    turn("what's the status of order 1234 and why?", db)
    tool_use = [b for row in db.rows for b in row["content"] if b.get("type") == "tool_use"][0]
    assert tool_use["thought_signature"] == "SIG_ABC"
    replayed = gemini["requests"][1]["contents"][1]["parts"][0]
    assert replayed["thoughtSignature"] == "SIG_ABC" and replayed["functionCall"]["name"] == "get_order"
    assert gemini["requests"][1]["contents"][2]["parts"][0]["functionResponse"]["response"]["status"] == "paid"


def test_foreign_tool_calls_get_placeholder_and_claude_copy_is_clean():
    hist = [{"role": "user", "content": [{"type": "text", "text": "q"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "get_order", "input": {}},
                                              {"type": "tool_use", "id": "t2", "name": "get_order", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"},
                                         {"type": "tool_result", "tool_use_id": "t2", "content": "{}"}]}]
    parts = gf._anthropic_messages_to_gemini(hist)[1]["parts"]
    assert parts[0]["thoughtSignature"] == gf.SKIP_SIGNATURE and "thoughtSignature" not in parts[1]
    block = {"type": "tool_use", "id": "x", "name": "n", "input": {}, "thought_signature": "S"}
    assert "thought_signature" not in json.dumps(agent._for_anthropic([{"role": "assistant", "content": [block]}]))


def test_errors_show_status_and_never_the_key(gemini):
    key = os.environ["GEMINI_API_KEY"]
    gemini["script"] = [(404, f'{{"error": {{"message": "not found. key={key}"}}}}')] * (2 * len(gf.GEMINI_MODELS))
    db = DB()
    with pytest.raises(agent.AgentUnavailableError) as e:
        turn("tell me something", db)
    assert "HTTP 404" in str(e.value) and "not found" in str(e.value)
    assert key not in str(e.value) and key not in json.dumps(db.rows)


def test_overloaded_model_falls_back_and_sticks(gemini):
    gemini["script"] = [(503, {"error": {"message": "high demand"}}), (200, call_resp("9", "S2")), (200, text_resp("done"))]
    r = turn("check order 9 and explain")
    m = gf.GEMINI_MODELS
    assert r["text"] == "done" and r["model"] == m[1]
    assert gemini["models"] == [m[0], m[1], m[1]]


def test_bad_request_is_not_retried(gemini):
    gemini["script"] = [(400, {"error": {"message": "bad signature"}})]
    with pytest.raises(agent.AgentUnavailableError) as e:
        turn("hello again friend")
    assert len(gemini["models"]) == 1 and "HTTP 400" in str(e.value)


def test_chat_cannot_see_or_call_automation_toggle():
    guarded = agent._GuardedTools(MCP())
    assert "set_automation_setting" not in [t.name for t in asyncio.run(guarded.list_tools())]
    with pytest.raises(PermissionError):
        asyncio.run(guarded.call_tool("set_automation_setting", {"confirm": True}))


def test_generate_text_returns_json_text(gemini):
    gemini["script"] = [(200, text_resp('{"title": "T", "description": "D"}'))]
    text, model = asyncio.run(gf.generate_text("write"))
    assert json.loads(text) == {"title": "T", "description": "D"} and model == gf.GEMINI_MODELS[0]
    assert gemini["requests"][0]["generationConfig"]["responseMimeType"] == "application/json"


def test_redact_covers_all_token_shapes():
    t = gf.redact("x?key=AIzaSECRETSECRETSECRETSECRET1 access_token=EAABsecret&y=1 "
                  "https://api.telegram.org/bot123456789:AAHsecretsecretsecretsecret/sendMessage")
    assert "SECRET1" not in t and "EAABsecret" not in t and "AAHsecret" not in t
