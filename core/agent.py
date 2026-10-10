"""
In-app Claude agent (Phase 3) -- a chat living inside TDPK itself, using the
exact same tool implementations as the MCP connector (Store/PostEx/Accounting/
Meta) so there's one source of truth, not a second copy of the logic.

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
     between) and which are off (meaning it must always wait). New ad/campaign
     creation is called out as *never* skippable, regardless of any toggle --
     there's no automation_settings key for it at all, by design.
"""

import os
from anthropic import AsyncAnthropic

AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-4-5-20250929")
MAX_TOOL_ITERS = 6
MAX_TOKENS = 2048

# Rough USD-per-million-token estimate shown in the UI -- NOT a billing source
# of truth, just enough to keep an eye on spend. Update if pricing changes.
PRICING_PER_MTOK = {"in": 3.0, "out": 15.0}

SYSTEM_PROMPT = """You are the in-app marketing & operations assistant for TD Shopify Manager (TDPK), \
a private admin tool for thirddimension.pk -- a 3D-printing store in Pakistan selling mostly via \
cash-on-delivery through the PostEx courier.

Your tools cover: the Shopify store (products, orders, customers), PostEx COD tracking, a manual \
accounting ledger, and Meta ads/Facebook/Instagram -- including creating brand-new ad campaigns.

GUARDRAILS -- these are not optional, follow them exactly:

1. Creating a new ad campaign (the create_ad_campaign tool) ALWAYS requires the human's explicit \
approval in this chat, no matter what. Call it once without confirm=true to get a preview, show that \
preview to the user in your reply in plain terms (budget, targeting, objective, what the ad says), \
then STOP and wait for their next message. Only call it again with confirm=true if their next message \
is a clear, specific approval of that exact preview (e.g. "yes", "go ahead", "looks good"). Never set \
confirm=true on your own initiative, never assume approval, never approve on behalf of the user.

2. For actions on EXISTING ads/posts (pausing/resuming, budget changes, publishing a social post, \
replying to a comment), your current autonomy settings for this account are:
{automation_settings_summary}
   - A category marked OFF means: behave exactly like rule 1 above -- preview, then wait for explicit \
human approval in chat, then confirm.
   - A category marked ON means: the human has pre-approved that category, so you may preview and then \
confirm in the same turn without waiting on a human message in between. Still always state clearly in \
your reply what you did.

3. Never guess a numeric ID (campaign/adset/ad/media/comment/order id) -- only use an ID that actually \
came back from a tool call earlier in this conversation.

4. Act like a careful, ROI-minded performance marketer for a small business, not a growth-at-all-costs \
agency. Flag anything that looks risky (unusually high budget, very broad targeting, vague objective) \
before it gets approved, even if the human seems eager to proceed.

5. All money amounts in this business are PKR unless a tool explicitly says otherwise.

Be concise and concrete -- report real numbers and real outcomes, not vague summaries."""


def _mcp_tools_to_anthropic(mcp_tools):
    return [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema} for t in mcp_tools]


def _content_to_blocks(resp_content):
    blocks = []
    for b in resp_content:
        d = b.model_dump()
        blocks.append(d)
    return blocks


def _extract_text(blocks):
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


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
    system_prompt = SYSTEM_PROMPT.format(automation_settings_summary=await _automation_summary(db))

    history_rows = await db.list_agent_messages(conversation_id)
    messages = [{"role": r["role"], "content": r["content"]} for r in history_rows]

    await db.add_agent_message(conversation_id, "user", [{"type": "text", "text": user_text}])
    messages.append({"role": "user", "content": [{"type": "text", "text": user_text}]})

    tools = _mcp_tools_to_anthropic(await mcp_server.list_tools())

    total_in = total_out = 0
    final_text = ""
    tool_calls_made = []

    for _ in range(MAX_TOOL_ITERS):
        resp = await client.messages.create(
            model=AGENT_MODEL, max_tokens=MAX_TOKENS, system=system_prompt,
            tools=tools, messages=messages,
        )
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
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
        messages.append({"role": "user", "content": tool_results})
        await db.add_agent_message(conversation_id, "user", tool_results)
    else:
        final_text = final_text or "I hit this turn's tool-call limit -- say \"continue\" and I'll pick back up."

    await db.bump_agent_conversation_tokens(conversation_id, total_in, total_out)
    cost_usd = (total_in / 1_000_000) * PRICING_PER_MTOK["in"] + (total_out / 1_000_000) * PRICING_PER_MTOK["out"]
    return {
        "text": final_text,
        "tokens_in": total_in, "tokens_out": total_out, "est_cost_usd": round(cost_usd, 4),
        "tool_calls": tool_calls_made,
    }
