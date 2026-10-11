"""
In-app Claude agent (Phase 3) -- the "TDPK Manager" chat living inside TDPK
itself, scoped to the WHOLE business (Shopify catalog/orders, PostEx,
accounting, ads/social), not just marketing. Uses the exact same tool
implementations as the MCP connector so there's one source of truth, not a
second copy of the logic.

How it reuses the MCP tools: `mcp_server` (the FastMCP instance built in
main.py) already has every tool registered with its schema. We hand its
tool list straight to Claude as the tool definitions, and execute a tool call
by calling `mcp_server.call_tool(name, args)` -- same function, same
confirm-gating, same preview-then-confirm behaviour as the MCP connector and
the dashboard's own buttons get. Nothing here duplicates a single API call.

Guardrails live in two places:
  1. The tools themselves -- any write action with a `confirm` parameter does
     nothing on the first call and returns a preview instead. That's
     unconditional, not something this module can bypass by accident.
  2. The system prompt -- told explicitly, every turn, which automation
     categories are currently toggled on (meaning the agent may confirm an
     action for that category without waiting for a fresh human "yes" in
     between) and which are off (meaning it must always wait).

Three tiers of write action, by risk:
  - Toggle-able (existing-ad pause/resume, budget change, social publish,
    comment reply): follows the live automation_settings state below.
  - ALWAYS approve, no toggle exists at all: new ad/campaign creation, bulk
    catalog operations (can touch hundreds of products), and order
    cancel/refund. These are called out explicitly in the prompt below and
    must never be auto-confirmed, regardless of any toggle.
"""

import os
from datetime import datetime, timezone, timedelta
from anthropic import AsyncAnthropic

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time, no DST -- fixed offset is correct year-round

AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-4-5-20250929")
MAX_TOOL_ITERS = 6
MAX_TOKENS = 2048

# How many of the most recent user-initiated turns get replayed to the model
# each call. Full history is always kept in Supabase for the dashboard
# transcript -- this only bounds what gets RESENT as input tokens, which
# otherwise grows without limit for a long-running conversation (most
# importantly the standing "Telegram" conversation, which never gets
# replaced with a fresh one the way a dashboard conversation does). Override
# with AGENT_HISTORY_TURNS if needed.
MAX_HISTORY_TURNS = int(os.getenv("AGENT_HISTORY_TURNS", "15") or 15)

# Tool results (e.g. list_orders, get_pnl) can be large. The live turn still
# sees the full result so its immediate answer is accurate, but only a
# truncated copy gets written to history -- otherwise every big read ever
# made in a conversation gets re-billed as input tokens on every future turn
# in that same conversation, forever.
MAX_STORED_TOOL_RESULT_CHARS = 4000

# Rough USD-per-million-token estimate shown in the UI -- NOT a billing source
# of truth, just enough to keep an eye on spend. Update if pricing changes.
# Cache writes cost 1.25x a normal input token (one-time, when a cache entry
# is first created); cache reads cost 0.1x a normal input token (every turn
# after that reuses the cached system prompt + tools for a tenth of the
# price) -- see Anthropic's prompt-caching pricing.
PRICING_PER_MTOK = {"in": 3.0, "out": 15.0, "cache_write": 3.75, "cache_read": 0.3}

# Split into a large STATIC block (cached -- identical on every call, so
# Anthropic's prompt caching reuses it instead of rebilling it as fresh input
# tokens each turn) and a small DYNAMIC block appended after (current time +
# live automation-toggle state, which do change turn to turn so must stay
# outside the cached portion).
SYSTEM_PROMPT_STATIC = """You are "TDPK Manager" -- the in-app assistant for TD Shopify Manager (TDPK), a \
private admin tool for thirddimension.pk, a 3D-printing store in Pakistan selling mostly via \
cash-on-delivery through the PostEx courier. You act as a general manager for the WHOLE business, not \
a marketing-only bot: the Shopify catalog and orders, PostEx COD logistics, the full accounting ledger \
(vendors, printers, custom orders, outsourced production, expenses, employees, cash accounts), filament \
raw-material inventory, and Meta ads/Facebook/Instagram are all equally your job, not a secondary \
feature bolted onto ads.

You will be told the current date/time (Pakistan local, UTC+5) below -- use it to resolve relative \
dates ("tomorrow", "in 3 days", "next Monday") into real ISO due_at values, never guess what day it is.

GUARDRAILS -- these are not optional, follow them exactly. There are three tiers of write action:

TIER 1 -- ALWAYS requires the human's explicit approval in THIS chat, no exceptions, no toggle can ever \
skip it. This covers: creating a new ad campaign (create_ad_campaign), any bulk catalog operation \
(run_bulk_catalog_operation -- colour/pricing/tag/publish/vendor/etc. changes across many products at \
once), and order-level actions (cancel_order, refund_order). For every one of these: call the tool once \
without confirm=true to get a preview, show that preview to the user in your reply in plain concrete \
terms (exact counts, exact amounts, exact products/orders affected), then STOP and wait for their next \
message. Only call it again with confirm=true if their next message is a clear, specific approval of \
THAT EXACT preview (e.g. "yes", "go ahead", "looks good"). Never set confirm=true on your own \
initiative, never assume approval, never approve on behalf of the user, never bundle approval across \
multiple different previews at once.

TIER 2 -- governed by a live on/off toggle. For actions on EXISTING ads/posts (pausing/resuming, \
budget changes, publishing a social post, replying to a comment), your current autonomy settings for \
this account are listed below, under "Current automation settings":
   - A category marked OFF means: behave exactly like Tier 1 above -- preview, then wait for explicit \
human approval in chat, then confirm.
   - A category marked ON means: the human has pre-approved that category, so you may preview and then \
confirm in the same turn without waiting on a human message in between. Still always state clearly in \
your reply what you did.

TIER 3 -- plain reads and routine single-item actions that don't need a confirm step at all (looking up \
orders, checking stock, logging an accounting entry, marking a single order fulfilled, logging a \
filament purchase, creating or cancelling a reminder). Just do these when asked.

OTHER RULES:

- Never guess a numeric ID (campaign/adset/ad/media/comment/order/product id) -- only use an ID that \
actually came back from a tool call earlier in this conversation.
- When touching ads specifically, act like a careful, ROI-minded performance marketer for a small \
business, not a growth-at-all-costs agency -- flag anything that looks risky (unusually high budget, \
very broad targeting, vague objective) before it gets approved.
- When touching the catalog or orders, act like a careful operations manager -- flag anything that \
looks risky (an "all products" scope, a very large price change, cancelling/refunding a high-value or \
already-fulfilled order) before it gets approved, even if the human seems eager to proceed.
- refund_order does NOT move money (this is a COD store, Shopify never captured a payment) -- it only \
restocks and records the return. If cash needs to go back to a customer, say so clearly and suggest \
logging it with add_cash_transaction.
- All money amounts in this business are PKR unless a tool explicitly says otherwise.
- You have NO memory or presence outside of being called for a turn -- you cannot "keep watching" \
something in the background on your own. If you tell the human you'll check on something later (an ad's \
performance tomorrow, whether a low-stock item got reordered, etc.), you MUST call create_reminder with \
a concrete due_at, or that promise will simply never happen. When a reminder fires, it's pushed straight \
to the human's phone (Telegram/email) -- you don't get a chance to re-check it yourself first, so word \
the reminder message as if it's being read standalone, with enough context to make sense on its own.
- A daily digest (orders, PostEx deliveries/returns, low stock, filament reorder needs) already goes out \
automatically on its own schedule -- don't create a reminder to replicate that, only for something \
specific to this conversation.
- Filament tracking is best-effort: orders that couldn't be auto-matched to a filament colour sit in \
list_filament_needs_review until manually resolved. If asked about filament stock, mention if there are \
needs-review items pending so the numbers aren't silently incomplete.

Be concise and concrete -- report real numbers and real outcomes, not vague summaries."""


def _mcp_tools_to_anthropic(mcp_tools):
    tools = [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema} for t in mcp_tools]
    if tools:
        # Marks the end of a cacheable segment -- the whole tool list (all 70+
        # schemas) gets reused from cache on the next call instead of being
        # rebilled as input tokens, as long as the tool list itself hasn't
        # changed since the last call.
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def _truncate_for_storage(text, limit=MAX_STORED_TOOL_RESULT_CHARS):
    """Caps what a tool result looks like once it's history (replayed to the
    model on every future turn in this conversation) -- the current turn
    still sees and reasons over the FULL result, only the stored copy is
    capped."""
    if not isinstance(text, str) or len(text) <= limit:
        return text
    cut = len(text) - limit
    return text[:limit] + f"\n...[truncated {cut} more characters -- re-run the tool call for the full result]"


def _window_messages(messages, max_turns=MAX_HISTORY_TURNS):
    """Keeps only the most recent `max_turns` user-initiated turns (a real
    human message, not a tool_result handed back mid-turn) and everything
    from that point on. Always cuts at a genuine turn boundary, so a
    tool_use/tool_result pair is never split across the cut. Full history
    stays in Supabase regardless -- this only bounds what gets resent to the
    model as input tokens."""
    turn_starts = [
        i for i, m in enumerate(messages)
        if m["role"] == "user" and isinstance(m["content"], list)
        and any(b.get("type") == "text" for b in m["content"])
    ]
    if len(turn_starts) <= max_turns:
        return messages
    return messages[turn_starts[-max_turns]:]


def _content_to_blocks(resp_content):
    blocks = []
    for b in resp_content:
        d = b.model_dump()
        blocks.append(d)
    return blocks


def _extract_text(blocks):
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def _derive_title(user_text):
    """Turns a user's first message into a short conversation title, so the
    sidebar doesn't fill up with indistinguishable "New conversation" rows.
    No extra model call -- just a cheap, deterministic truncation."""
    collapsed = " ".join(user_text.split())
    if len(collapsed) > 48:
        collapsed = collapsed[:45].rstrip() + "..."
    return collapsed or "New conversation"


async def _automation_summary(db):
    try:
        settings = await db.get_automation_settings()
    except Exception:
        settings = {}
    if not settings:
        return "   (could not load automation settings -- treat everything as OFF/ask-first)"
    lines = []
    for key, s in settings.items():
        state = "ON (can act without asking first)" if s.get("enabled") else "OFF (must ask first)"
        lines.append(f"   - {key}: {state}")
    return "\n".join(lines)


async def run_agent_turn(mcp_server, db, conversation_id, user_text):
    """Runs one user turn through to a final assistant reply, executing any
    tool calls Claude makes along the way. Persists every message (user,
    assistant text/tool_use, and tool_result) so the conversation can be
    reloaded and continued later exactly as Anthropic's API expects it."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set on the server -- add it on Render to use the agent.")

    client = AsyncAnthropic(api_key=api_key)
    now_pkt = datetime.now(timezone.utc).astimezone(PKT).strftime("%Y-%m-%d %H:%M (%A)")
    dynamic_context = (
        f"Current date/time (Pakistan local, UTC+5): {now_pkt}.\n\n"
        f"Current automation settings:\n{await _automation_summary(db)}"
    )
    # Two blocks: the big static instructions (cached -- reused across calls
    # instead of rebilled every turn) and this small per-turn block (never
    # cached, since it changes turn to turn).
    system_prompt = [
        {"type": "text", "text": SYSTEM_PROMPT_STATIC, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic_context},
    ]

    history_rows = await db.list_agent_messages(conversation_id)
    full_messages = [{"role": r["role"], "content": r["content"]} for r in history_rows]
    is_first_message = not history_rows

    await db.add_agent_message(conversation_id, "user", [{"type": "text", "text": user_text}])
    full_messages.append({"role": "user", "content": [{"type": "text", "text": user_text}]})

    # Only the most recent MAX_HISTORY_TURNS get sent to the model -- bounds
    # input-token cost for long-running conversations (the standing Telegram
    # thread especially). Full history is still in Supabase regardless.
    messages = _window_messages(full_messages)

    tools = _mcp_tools_to_anthropic(await mcp_server.list_tools())

    total_in = total_out = total_cache_write = total_cache_read = 0
    final_text = ""
    tool_calls_made = []

    for _ in range(MAX_TOOL_ITERS):
        resp = await client.messages.create(
            model=AGENT_MODEL, max_tokens=MAX_TOKENS, system=system_prompt,
            tools=tools, messages=messages,
        )
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        total_cache_write += getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
        total_cache_read += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        blocks = _content_to_blocks(resp.content)
        messages.append({"role": "assistant", "content": blocks})
        await db.add_agent_message(conversation_id, "assistant", blocks,
                                    tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens)

        if resp.stop_reason != "tool_use":
            final_text = _extract_text(blocks)
            break

        tool_results = []
        for block in blocks:
            if block.get("type") != "tool_use":
                continue
            tool_calls_made.append({"name": block["name"], "input": block.get("input", {})})
            try:
                result = await mcp_server.call_tool(block["name"], block.get("input", {}) or {})
                result_text = "\n".join(getattr(c, "text", str(c)) for c in result) if hasattr(result, "__iter__") else str(result)
            except Exception as e:
                result_text = f"Error: {e}"
            tool_results.append({"type": "tool_result", "tool_use_id": block["id"], "content": result_text})
        # The live turn keeps the full-size results (messages/tools list) so
        # its own answer this turn is accurate; only the copy written to
        # history is capped, so a big read made now doesn't get rebilled at
        # full size on every future turn in this conversation.
        messages.append({"role": "user", "content": tool_results})
        stored_tool_results = [
            {**tr, "content": _truncate_for_storage(tr["content"])} for tr in tool_results
        ]
        await db.add_agent_message(conversation_id, "user", stored_tool_results)
    else:
        final_text = final_text or "I hit this turn's tool-call limit -- say \"continue\" and I'll pick back up."

    title = None
    if is_first_message:
        try:
            conv = await db.get_agent_conversation(conversation_id)
        except Exception:
            conv = None
        current_title = (conv or {}).get("title")
        if not current_title or current_title == "New conversation":
            title = _derive_title(user_text)
    # total_in from the API already excludes cache_read tokens (counted
    # separately), so this sums all four buckets at their own real price
    # instead of treating a cheap cache-read token as a full-price one.
    await db.bump_agent_conversation_tokens(conversation_id, total_in, total_out, title=title)
    cost_usd = (
        (total_in / 1_000_000) * PRICING_PER_MTOK["in"]
        + (total_out / 1_000_000) * PRICING_PER_MTOK["out"]
        + (total_cache_write / 1_000_000) * PRICING_PER_MTOK["cache_write"]
        + (total_cache_read / 1_000_000) * PRICING_PER_MTOK["cache_read"]
    )
    return {
        "text": final_text,
        "tokens_in": total_in, "tokens_out": total_out,
        "cache_write_tokens": total_cache_write, "cache_read_tokens": total_cache_read,
        "est_cost_usd": round(cost_usd, 4),
        "tool_calls": tool_calls_made,
    }
