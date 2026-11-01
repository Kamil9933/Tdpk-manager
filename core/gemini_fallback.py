"""
Google Gemini driver for the TDPK Manager agent. As of the cost-control
rework in core/agent.py, this is the DEFAULT model for any turn Tier 1
(core/rules.py) didn't handle with zero LLM cost -- not a last-resort
fallback any more. Claude is the one held in reserve now (used only if a
message explicitly asks for it, or if Gemini itself can't be reached) --
see core/agent.py's module docstring for the full routing chain. Routes a
turn through the SAME MCP tool registry and the SAME guardrails (a write
tool with a `confirm` param still does nothing on the first call,
unconditionally, regardless of which model is driving) via direct REST
calls to Google's API (no SDK dependency, httpx is already a project
dependency).

Needs GEMINI_API_KEY set on Render (free tier covers light usage).
GEMINI_FALLBACK_MODEL to change the model (default: a fast/free-tier-eligible
one).

Known limitations -- this is a cheaper default, not a Claude-parity guarantee:
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
import re
import json
import asyncio
import httpx

# GEMINI_FALLBACK_MODEL may be one model or a comma-separated list; the
# defaults below are appended after it as backups. Google returns 503 "high
# demand" for a single model fairly often, so one turn tries each model in
# order and sticks with the first that answers.
_DEFAULT_MODELS = ["gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-flash-latest", "gemini-3.5-flash-lite"]
GEMINI_MODELS = list(dict.fromkeys(
    [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODEL", "").split(",") if m.strip()] + _DEFAULT_MODELS
))
GEMINI_MODEL = GEMINI_MODELS[0]
# Overloaded / rate-limited / server-side: worth trying the next model.
# 404 too, since a model can be unavailable for a given key.
GEMINI_RETRYABLE_STATUSES = {404, 429, 500, 502, 503, 504}
GEMINI_RETRY_PAUSE_SECONDS = 3
GEMINI_MAX_TOOL_ITERS = 6
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_ERROR_BODY_CHARS = 300

# Gemini 3 models attach a thoughtSignature to functionCall parts and reject
# (HTTP 400) a replayed history whose function calls are missing it. We keep
# Gemini's own signature on the stored tool_use block ("thought_signature") and
# send it back as received. Function calls Gemini never produced (Claude- or
# rules.py-authored tool_use blocks) have no real signature, so they get
# Google's documented placeholder instead.
SKIP_SIGNATURE = "skip_thought_signature_validator"

_KEY_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),           # Google API key shape
    re.compile(r"([?&]key=)[^&\s\"']+", re.I),         # key in a query string
    re.compile(r"(access_token=)[^&\s\"']+", re.I),    # Meta Graph API token in a URL
    re.compile(r"bot\d{6,}:[A-Za-z0-9_\-]{20,}"),       # Telegram bot token in a URL
]


class GeminiError(RuntimeError):
    """Gemini call failed. Message is already redacted and safe to show in
    the chat or store in Supabase (HTTP status + start of the response body,
    never the request URL)."""
    pass


def redact(text) -> str:
    """Strips secrets (GEMINI_API_KEY, anything shaped like a Google API key,
    Meta access tokens, Telegram bot tokens) from error text before it
    reaches the chat UI, Telegram, or the database. Used app-wide, not just
    for Gemini errors."""
    text = str(text)
    key = os.getenv("GEMINI_API_KEY", "")
    if key:
        text = text.replace(key, "[REDACTED]")
    text = _KEY_PATTERNS[0].sub("[REDACTED]", text)
    text = _KEY_PATTERNS[1].sub(r"\1[REDACTED]", text)
    text = _KEY_PATTERNS[2].sub(r"\1[REDACTED]", text)
    text = _KEY_PATTERNS[3].sub("bot[REDACTED]", text)
    return text


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
        first_call_in_turn = True
        for b in content:
            t = b.get("type")
            if t == "text" and b.get("text"):
                parts.append({"text": b["text"]})
            elif t == "tool_use":
                part = {"functionCall": {"name": b["name"], "args": b.get("input") or {}}}
                sig = b.get("thought_signature")
                if sig:
                    part["thoughtSignature"] = sig
                elif first_call_in_turn:
                    # Only the first call of a parallel batch carries a signature.
                    part["thoughtSignature"] = SKIP_SIGNATURE
                first_call_in_turn = False
                parts.append(part)
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


async def _generate(client, api_key, body, models):
    """POSTs one generateContent call, walking `models` until one answers.
    Retryable failures (overload, rate limit, 5xx, unknown model) move on to
    the next model; after a full pass with nothing but retryable failures it
    pauses once and tries the whole list again. Anything else (bad request,
    bad key) raises straight away. Returns (response_json, model_used)."""
    failures, last_detail = [], ""
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(GEMINI_RETRY_PAUSE_SECONDS)
        for model in models:
            # Key goes in a header, not the URL, so httpx errors can't echo it.
            try:
                resp = await client.post(
                    f"{GEMINI_API_BASE}/models/{model}:generateContent",
                    headers={"x-goog-api-key": api_key}, json=body,
                )
            except httpx.HTTPError as e:
                failures.append(f"{model}: {type(e).__name__}")
                continue
            if resp.status_code < 400:
                return resp.json(), model
            detail = f"Gemini HTTP {resp.status_code} ({model}): {resp.text[:GEMINI_ERROR_BODY_CHARS]}"
            if resp.status_code not in GEMINI_RETRYABLE_STATUSES:
                raise GeminiError(redact(detail))
            failures.append(f"{model}: HTTP {resp.status_code}")
            last_detail = detail
    # Last real error first: the chat message is truncated, and Google's own
    # text is the useful part. Then which models were tried.
    tried = "; ".join(dict.fromkeys(failures))
    head = f"every Gemini model failed, last error: {last_detail}" if last_detail else "every Gemini model failed"
    raise GeminiError(redact(f"{head} [tried: {tried}]"))


async def generate_text(prompt):
    """One plain text-in/text-out Gemini call (no tools, no history), with the
    same model fallback as the chat. Asks for JSON output. Returns
    (text, model_used)."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise GeminiError("GEMINI_API_KEY is not set -- can't use Gemini.")
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"}}
    async with httpx.AsyncClient(timeout=60) as client:
        data, model = await _generate(client, api_key, body, list(GEMINI_MODELS))
    parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought")), model


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
        raise GeminiError("GEMINI_API_KEY is not set -- can't use Gemini.")

    tools = _mcp_tools_to_gemini(mcp_tools)
    models = list(GEMINI_MODELS)
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
            data, model_used = await _generate(client, api_key, body, models)
            # Stick with whichever model answered for the rest of this turn.
            models = [model_used] + [m for m in models if m != model_used]
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
                if p.get("thought"):
                    continue  # thought summaries, if ever enabled, aren't part of the reply
                if "text" in p and p["text"]:
                    blocks.append({"type": "text", "text": p["text"]})
                elif "functionCall" in p:
                    fc = p["functionCall"]
                    call_id = f"gemini_{len(function_calls)}_{fc['name']}"
                    block = {"type": "tool_use", "id": call_id, "name": fc["name"], "input": fc.get("args") or {}}
                    if p.get("thoughtSignature"):
                        block["thought_signature"] = p["thoughtSignature"]
                    blocks.append(block)
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
        "provider": "gemini", "model": models[0],
    }
