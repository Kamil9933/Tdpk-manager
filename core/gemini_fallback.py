"""
Secondary LLM fallback for the TDPK Manager agent -- used ONLY when the
primary Anthropic call fails (out of credit, rate-limited, momentarily down)
and the rule-based yes/no bypass in core/agent.py doesn't apply (i.e. this is
a brand-new instruction, not just confirming/cancelling something already
previewed). Routes that SAME turn -- same MCP tool registry, same guardrails
(a write tool with a `confirm` param still does nothing on the first call,
unconditionally, regardless of which model is driving) -- through Google's
Gemini API instead, via direct REST calls (no SDK dependency, httpx is
already a project dependency) so the Manager keeps working end-to-end during
an Anthropic outage/empty balance rather than just failing.

Needs GEMINI_API_KEY set on Render (free tier covers light usage).
GEMINI_FALLBACK_MODEL to change the model (default: a fast/free-tier-eligible
one).

Known limitations -- this is a fallback, not a parity guarantee:
  - Gemini's own judgement about when to ask for confirmation, or how it
    phrases things, can differ from Claude's. The TOOL-LEVEL guardrail
    (confirm=false always returns a preview, never executes) is what
    actually keeps destructive actions safe, and that's unconditional either
    way -- this fallback can't bypass it any more than Claude can.
  - Schema translation (Anthropic/MCP's JSON Schema -> Gemini's OpenAPI-subset
    format) is best-effort, not a full JSON Schema implementation -- see
    _sanitize_schema. It covers every tool schema this project currently
    defines (plain object/string/integer/number/boolean/array types).
  - Conversation history is still stored in the same Anthropic-shaped format
    this app already uses everywhere else (so Claude can pick the thread
    back up once it's available again) -- translated to/from Gemini's wire
    format on the fly here, never stored natively in Gemini's shape.
"""
import os
import json
import httpx

GEMINI_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.0-flash")
GEMINI_MAX_TOOL_ITERS = 6
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


def is_configured() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))


def _sanitize_schema(schema):
    """Best-effort translation from the JSON Schema MCP tools already use to
    Gemini's OpenAPI-3-subset function-calling schema: uppercase type enums,
    and drop a few JSON Schema keywords Gemini's schema doesn't understand."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k in ("$schema", "additionalProperties", "title", "default"):
            continue
        if k == "type" and isinstance(v, str):
            out[k] = v.upper()
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: _sanitize_schema(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = _sanitize_schema(v)
        elif k in ("anyOf", "oneOf") and isinstance(v, list):
            # Gemini's schema has no real union support -- collapse to the
            # first non-null variant as a reasonable best-effort stand-in.
            non_null = [s for s in v if isinstance(s, dict) and s.get("type") != "null"]
            if non_null:
                out.update(_sanitize_schema(non_null[0]))
            continue
        else:
            out[k] = v
    if out.get("type") == "OBJECT" and "properties" not in out:
        out["properties"] = {}
    return out


def _mcp_tools_to_gemini(mcp_tools):
    decls = [
        {
            "name": t.name,
            "description": (t.description or "")[:1000],
            "parameters": _sanitize_schema(t.inputSchema or {"type": "object", "properties": {}}),
        }
        for t in mcp_tools
    ]
    return [{"functionDeclarations": decls}] if decls else []


def _anthropic_messages_to_gemini(messages):
    """Translates the SAME Anthropic-shaped `messages` list run_agent_turn
    already builds (text / tool_use / tool_result blocks, the exact format
    stored in Supabase) into Gemini's `contents` (role user/model, parts of
    text / functionCall / functionResponse). A tool_result block only carries
    the originating tool_use_id, not the tool's name -- Gemini needs the
    NAME on functionResponse, not an id -- so this first builds an id->name
    map from every tool_use block in the list (Claude- or Gemini-authored
    alike, since both are stored in this same shape) before translating."""
    id_to_name = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for b in (m.get("content") or []):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                id_to_name[b.get("id")] = b.get("name")

    contents = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        parts = []
        content = m.get("content")
        if not isinstance(content, list):
            content = [{"type": "text", "text": content}]
        for b in content:
            t = b.get("type")
            if t == "text" and b.get("text"):
                parts.append({"text": b["text"]})
            elif t == "tool_use":
                parts.append({"functionCall": {"name": b["name"], "args": b.get("input") or {}}})
            elif t == "tool_result":
                raw = b.get("content")
                try:
                    payload = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    payload = {"result": raw}
                if not isinstance(payload, dict):
                    payload = {"result": payload}
                name = id_to_name.get(b.get("tool_use_id"), "tool")
                parts.append({"functionResponse": {"name": name, "response": payload}})
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


async def run_gemini_turn(mcp_server, db, conversation_id, system_text, messages, mcp_tools):
    """Mirrors core.agent.run_agent_turn's tool-calling loop against Gemini
    instead of Anthropic. `messages` is the SAME Anthropic-shaped history
    run_agent_turn already assembled for this turn (including the just-added
    user message, already windowed) -- this only translates the wire format,
    it doesn't re-derive history. Returns the same result shape
    run_agent_turn does, so callers don't need to special-case it, plus
    "provider": "gemini" so it's visible which model actually answered."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set -- can't use the Gemini fallback.")

    tools = _mcp_tools_to_gemini(mcp_tools)
    final_text = ""
    tool_calls_made = []
    local_messages = list(messages)  # Anthropic-shaped; translated to Gemini's wire format fresh each call

    async with httpx.AsyncClient(timeout=60) as client:
        for _ in range(GEMINI_MAX_TOOL_ITERS):
            body = {
                "contents": _anthropic_messages_to_gemini(local_messages),
                "systemInstruction": {"parts": [{"text": system_text}]},
            }
            if tools:
                body["tools"] = tools
            resp = await client.post(
                f"{GEMINI_API_BASE}/models/{GEMINI_MODEL}:generateContent",
                params={"key": api_key}, json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                final_text = "Gemini returned no response (possibly blocked by its own safety filters)."
                break
            parts = (candidates[0].get("content") or {}).get("parts") or []

            # Store this turn's reply in the SAME Anthropic-shaped format the
            # rest of the app (and Claude, next time it's available) expects.
            blocks = []
            function_calls = []
            for p in parts:
                if "text" in p and p["text"]:
                    blocks.append({"type": "text", "text": p["text"]})
                elif "functionCall" in p:
                    fc = p["functionCall"]
                    call_id = f"gemini_{len(function_calls)}_{fc['name']}"
                    blocks.append({"type": "tool_use", "id": call_id, "name": fc["name"], "input": fc.get("args") or {}})
                    function_calls.append((call_id, fc["name"], fc.get("args") or {}))
            local_messages.append({"role": "assistant", "content": blocks})
            await db.add_agent_message(conversation_id, "assistant", blocks)

            if not function_calls:
                final_text = "\n".join(b["text"] for b in blocks if b["type"] == "text").strip()
                break

            tool_results = []
            for call_id, name, args in function_calls:
                tool_calls_made.append({"name": name, "input": args})
                try:
                    result = await mcp_server.call_tool(name, args)
                    result_text = "\n".join(getattr(c, "text", str(c)) for c in result) if hasattr(result, "__iter__") else str(result)
                except Exception as e:
                    result_text = json.dumps({"error": str(e)})
                tool_results.append({"type": "tool_result", "tool_use_id": call_id, "content": result_text})
            local_messages.append({"role": "user", "content": tool_results})
            await db.add_agent_message(conversation_id, "user", tool_results)
        else:
            final_text = final_text or "Hit this turn's tool-call limit via the Gemini fallback -- say \"continue\" once Anthropic is back to pick it back up properly."

    return {
        "text": final_text, "tokens_in": 0, "tokens_out": 0,
        "cache_write_tokens": 0, "cache_read_tokens": 0,
        "est_cost_usd": 0, "tool_calls": tool_calls_made,
        "provider": "gemini",
    }
