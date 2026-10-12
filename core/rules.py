"""
Tier 1 of the Manager's model-routing chain (see core/agent.py's module
docstring for the full chain: Tier 1 here -- zero LLM cost -- then Gemini as
the new default driver, then Claude reserved for when Gemini can't be
reached or you explicitly ask for it in a message).

Handles routine, unambiguous chat commands WITHOUT calling any LLM at all --
instant, free. Triggered by a real request: Anthropic credit is expensive and
most day-to-day messages ("order 1234", "low stock", "fulfill order 1234")
don't need any model's judgement at all, just a direct lookup or action.

Design rule, non-negotiable: every matcher here is conservative. If a
message could plausibly mean more than one thing (a product name matches two
products, a price update's target variant is ambiguous), the handler returns
None and the caller falls through to Gemini instead of guessing -- a missed
match costs a few cents of Gemini, a wrong match could touch the wrong order
or the wrong price.

Write actions here follow the SAME preview-then-confirm storage shape
core/agent.py's pending-confirmation scanner (_find_pending_confirmation)
already understands: a plain "yes"/"no" reply closes one of these previews
out exactly the same way it closes out a preview Claude or Gemini opened --
same bypass, same zero LLM cost, regardless of which layer opened it.
"""
import json
import re
from datetime import datetime, timedelta, timezone

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time, fixed UTC+5

HELP_TEXT = (
    "Here's what I can do instantly, no AI needed (anything else goes to Gemini, "
    "or Claude if you say \"use claude\"):\n\n"
    "Look things up:\n"
    "  order 1234  /  #1234 -- order details\n"
    "  track <tracking number> -- PostEx tracking timeline\n"
    "  today's orders -- today's orders + total\n"
    "  low stock -- low-stock products\n"
    "  filament status -- filament stock + reorder suggestions\n"
    "  printer status -- live printer tiles\n"
    "  queue -- Print Queue summary\n"
    "  pnl -- this month's profit & loss\n"
    "  postex -- delivery/return snapshot\n"
    "  reminders -- your open reminders\n"
    "  store overview -- store snapshot\n"
    "  abandoned checkouts -- abandoned carts\n"
    "  expenses  /  vendors  /  employees  /  cash -- ledger reads\n"
    "  ad insights -- ad performance\n\n"
    "Do things (still previews first, just like the dashboard):\n"
    "  fulfill order 1234  /  ship order 1234 tracking TRK123\n"
    "  cancel order 1234\n"
    "  refund order 1234\n"
    "  set price of <product name> to <amount>\n\n"
    "Anything more involved (bulk changes, new ad campaigns, questions needing real "
    "judgement) goes to Gemini automatically. Say \"use claude\" in a message to route "
    "that one turn to Claude instead."
)


async def _call_tool(mcp_server, name, args):
    result = await mcp_server.call_tool(name, args)
    text = "\n".join(getattr(c, "text", str(c)) for c in result) if hasattr(result, "__iter__") else str(result)
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text}


def _money(v):
    try:
        return f"Rs {float(v):,.0f}"
    except Exception:
        return str(v)


# ───────────────────────── read-only lookups ─────────────────────────

async def _h_help(m, mcp_server, db):
    return HELP_TEXT


async def _h_order(m, mcp_server, db):
    order_id = int(m.group(1))
    data = await _call_tool(mcp_server, "get_order", {"order_id": order_id})
    if not data or data.get("error") or not data.get("id"):
        return f"Couldn't find order {order_id}."
    items = data.get("line_items", [])
    lines = [f"  - {li.get('quantity')}x {li.get('title')}" for li in items[:10]]
    customer = data.get("customer") or {}
    return (
        f"Order {data.get('name', order_id)} -- {data.get('financial_status','?')}/"
        f"{data.get('fulfillment_status') or 'unfulfilled'}\n"
        f"Customer: {customer.get('first_name','')} {customer.get('last_name','')}\n"
        f"Total: {_money(data.get('total_price'))}\n"
        f"City: {(data.get('shipping_address') or {}).get('city','?')}\n"
        + "\n".join(lines)
    )


async def _h_track(m, mcp_server, db):
    tn = m.group(1)
    data = await _call_tool(mcp_server, "track_postex_order", {"tracking_number": tn})
    if not data or data.get("error"):
        return f"Couldn't find tracking info for {tn}."
    return f"Tracking {tn}:\n{json.dumps(data, indent=2)[:1200]}"


async def _h_today_orders(m, mcp_server, db):
    today = datetime.now(PKT).date().isoformat()
    data = await _call_tool(mcp_server, "list_orders", {"from_date": today, "to_date": today, "limit": 50})
    orders = data.get("orders", [])
    if not orders:
        return "No orders yet today."
    total = sum(float(o.get("total_price") or 0) for o in orders)
    lines = [f"  - {o['name']}: {_money(o['total_price'])} ({o.get('city') or '?'})" for o in orders[:15]]
    return f"{len(orders)} order(s) today, total {_money(total)}:\n" + "\n".join(lines)


async def _h_low_stock(m, mcp_server, db):
    data = await _call_tool(mcp_server, "list_low_stock_products", {"threshold": 5})
    low = data.get("low_stock", [])
    if not low:
        return "Nothing at or below the low-stock threshold right now."
    lines = [f"  - {i['title']} ({i['variant']}): {i['stock']} left" for i in low[:20]]
    return f"{len(low)} low-stock variant(s):\n" + "\n".join(lines)


async def _h_filament(m, mcp_server, db):
    data = await _call_tool(mcp_server, "get_filament_status", {})
    status = data.get("status", [])
    if not status:
        return "No filament types registered yet."
    lines = []
    for s in status:
        lines.append(
            f"  - {s.get('color','?')} {s.get('material','?')}: {s.get('stock_grams','?')}g, "
            f"~{s.get('days_left','?')} days left, suggest {s.get('suggested_spools','?')} spool(s)"
        )
    return "Filament status:\n" + "\n".join(lines)


async def _h_printers(m, mcp_server, db):
    data = await _call_tool(mcp_server, "list_printers", {})
    printers = data.get("printers", [])
    if not printers:
        return "No printers registered."
    lines = []
    for p in printers:
        live = p.get("live_status") or {}
        state = live.get("gcode_state", "unknown")
        pct = live.get("percent")
        lines.append(f"  - {p.get('name')}: {state}" + (f", {pct}%" if pct is not None else ""))
    return "Printer status:\n" + "\n".join(lines)


async def _h_queue(m, mcp_server, db):
    try:
        rows = await db.list_queue()
    except Exception:
        return "Couldn't load the print queue right now."
    if not rows:
        return "Print queue is empty."
    counts = {}
    for r in rows:
        counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
    lines = [f"  - {k}: {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    return "Print queue:\n" + "\n".join(lines)


async def _h_pnl(m, mcp_server, db):
    data = await _call_tool(mcp_server, "get_pnl", {})
    return (
        f"P&L (last 30 days):\n"
        f"  Revenue: {_money(data.get('revenue'))}\n"
        f"  COGS: {_money(data.get('cogs'))}\n"
        f"  Opex: {_money(data.get('opex'))}\n"
        f"  Net profit: {_money(data.get('net_profit'))} ({data.get('margin_pct','?')}% margin)"
    )


async def _h_postex(m, mcp_server, db):
    data = await _call_tool(mcp_server, "get_postex_summary", {})
    return (
        f"PostEx (last 30 days):\n"
        f"  Delivered: {data.get('delivered_count','?')}\n"
        f"  Returned: {data.get('returned_count','?')}\n"
        f"  In transit: {data.get('in_transit_count','?')}\n"
        f"  Total COD value: {_money(data.get('total_cod_value'))}"
    )


async def _h_reminders(m, mcp_server, db):
    data = await _call_tool(mcp_server, "list_reminders", {"status": "pending"})
    rems = data.get("reminders", [])
    if not rems:
        return "No open reminders."
    lines = [f"  - {r.get('due_at')}: {r.get('message')}" for r in rems[:15]]
    return f"{len(rems)} open reminder(s):\n" + "\n".join(lines)


async def _h_overview(m, mcp_server, db):
    data = await _call_tool(mcp_server, "get_store_overview", {})
    p = data.get("products", {})
    o = data.get("orders", {})
    return (
        f"Store overview:\n"
        f"  Products: {p.get('total','?')} total, {p.get('active','?')} active, "
        f"{p.get('out_of_stock','?')} out of stock\n"
        f"  Orders (last {o.get('days','?')}d): {o.get('count','?')}, revenue {_money(o.get('revenue'))}"
    )


async def _h_abandoned(m, mcp_server, db):
    data = await _call_tool(mcp_server, "get_abandoned_checkouts", {})
    return f"{data.get('total_count',0)} abandoned checkout(s), total value {_money(data.get('total_value'))}."


async def _h_expenses(m, mcp_server, db):
    today = datetime.now(PKT).date()
    data = await _call_tool(mcp_server, "list_expenses", {
        "from_date": (today - timedelta(days=30)).isoformat(), "to_date": today.isoformat(),
    })
    rows = data.get("expenses", [])
    if not rows:
        return "No expenses logged in the last 30 days."
    total = sum(float(r.get("amount") or 0) for r in rows)
    return f"{len(rows)} expense(s) in the last 30 days, total {_money(total)}."


async def _h_vendors(m, mcp_server, db):
    data = await _call_tool(mcp_server, "list_vendors", {})
    rows = data.get("vendors", [])
    if not rows:
        return "No vendors on record."
    return f"{len(rows)} vendor(s): " + ", ".join(v.get("name", "?") for v in rows[:20])


async def _h_employees(m, mcp_server, db):
    data = await _call_tool(mcp_server, "list_employees", {})
    rows = data.get("employees", [])
    if not rows:
        return "No employees on record."
    return f"{len(rows)} employee(s): " + ", ".join(e.get("name", "?") for e in rows[:20])


async def _h_cash(m, mcp_server, db):
    data = await _call_tool(mcp_server, "list_cash_accounts", {})
    rows = data.get("accounts", [])
    if not rows:
        return "No cash accounts on record."
    lines = [f"  - {a.get('name','?')}: {_money(a.get('_balance'))}" for a in rows]
    return "Cash accounts:\n" + "\n".join(lines)


async def _h_ad_insights(m, mcp_server, db):
    data = await _call_tool(mcp_server, "get_ad_insights", {})
    if data.get("error"):
        return f"Couldn't fetch ad insights: {data['error']}"
    return f"Ad insights (last 7 days): {json.dumps(data, indent=2)[:1000]}"


# ───────────────────────── write actions ─────────────────────────
# Each opens the SAME kind of preview-then-confirm the dashboard/Claude/
# Gemini already use; a plain "yes"/"no" afterwards is caught by
# core.agent._try_confirm_bypass (runs on every turn, not just on outage),
# zero LLM cost either way.

async def _open_preview(db, conversation_id, tool_name, tool_input, preview_json, label):
    call_id = f"tier1_{tool_name}_{int(datetime.now(timezone.utc).timestamp()*1000)}"
    text = (
        f"{label}\n\n```\n{json.dumps(preview_json, indent=2)[:1200]}\n```\n\n"
        f"Reply yes to confirm or no to cancel."
    )
    await db.add_agent_message(conversation_id, "assistant", [
        {"type": "text", "text": text},
        {"type": "tool_use", "id": call_id, "name": tool_name, "input": tool_input},
    ])
    await db.add_agent_message(conversation_id, "user", [
        {"type": "tool_result", "tool_use_id": call_id, "content": json.dumps(preview_json)},
    ])
    return text


async def _h_fulfill(m, mcp_server, db, conversation_id):
    order_id = int(m.group(1))
    tracking = (m.group(2) or "").strip()
    result = await _call_tool(mcp_server, "fulfill_order", {"order_id": order_id, "tracking_number": tracking})
    if result.get("error"):
        return f"Couldn't fulfill order {order_id}: {result['error']}"
    return f"Fulfilled order {order_id}" + (f", tracking {tracking}" if tracking else "") + "."


async def _h_cancel(m, mcp_server, db, conversation_id):
    order_id = int(m.group(1))
    preview = await _call_tool(mcp_server, "cancel_order", {"order_id": order_id, "confirm": False})
    if not preview.get("preview"):
        return preview.get("error") or f"Couldn't preview cancelling order {order_id}."
    details = preview.get("details", {})
    label = f"About to cancel order {details.get('order_number', order_id)} ({_money(details.get('total'))})."
    return await _open_preview(
        db, conversation_id, "cancel_order", {"order_id": order_id, "confirm": False}, preview, label,
    )


async def _h_refund(m, mcp_server, db, conversation_id):
    order_id = int(m.group(1))
    preview = await _call_tool(mcp_server, "refund_order", {"order_id": order_id, "confirm": False})
    if not preview.get("preview"):
        return preview.get("error") or f"Couldn't preview refunding order {order_id}."
    details = preview.get("details", {})
    label = f"About to refund/return order {details.get('order_number', order_id)} (no money moves -- COD store)."
    return await _open_preview(
        db, conversation_id, "refund_order", {"order_id": order_id, "confirm": False}, preview, label,
    )


async def _h_set_price(m, mcp_server, db, conversation_id):
    name = m.group(1).strip()
    try:
        price = float(m.group(2))
    except Exception:
        return None
    search = await _call_tool(mcp_server, "search_products", {"search": name, "limit": 10})
    products = search.get("products", [])
    if len(products) != 1:
        return None  # no match or ambiguous -- let Gemini ask you to clarify instead of guessing
    product_id = products[0]["id"]
    detail = await _call_tool(mcp_server, "get_product", {"product_id": product_id})
    variants = detail.get("variants", [])
    if len(variants) != 1:
        return None  # multiple variants -- which one is ambiguous, don't guess
    variant = variants[0]
    preview = await _call_tool(mcp_server, "update_variant_price", {
        "product_id": product_id, "variant_id": variant["id"], "price": str(price), "confirm": False,
    })
    if not preview.get("preview"):
        return "Couldn't preview that price change."
    label = (
        f"About to change the price of {products[0]['title']} ({variant.get('title')}) from "
        f"{_money(variant.get('price'))} to {_money(price)}."
    )
    return await _open_preview(
        db, conversation_id, "update_variant_price",
        {"product_id": product_id, "variant_id": variant["id"], "price": str(price), "confirm": False},
        preview, label,
    )


# ───────────────────────── pattern registry ─────────────────────────

_READ_PATTERNS = [
    (re.compile(r"^\s*(?:help|commands|what can you do|list commands)\s*\??\s*$", re.I), _h_help),
    (re.compile(r"^\s*(?:check\s+)?order\s*#?\s*(\d+)\s*$", re.I), _h_order),
    (re.compile(r"^\s*#(\d+)\s*$"), _h_order),
    (re.compile(r"^\s*track\s+([A-Za-z0-9\-]{5,})\s*$", re.I), _h_track),
    (re.compile(r"^\s*(?:today'?s?\s+orders|orders\s+today)\s*$", re.I), _h_today_orders),
    (re.compile(r"^\s*(?:low\s*stock|stock\s*check)\s*$", re.I), _h_low_stock),
    (re.compile(r"^\s*filament(?:\s+status)?\s*$", re.I), _h_filament),
    (re.compile(r"^\s*(?:printer\s*status|printers)\s*$", re.I), _h_printers),
    (re.compile(r"^\s*(?:print\s+)?queue(?:\s+status)?\s*$", re.I), _h_queue),
    (re.compile(r"^\s*(?:p\s*&?\s*l|pnl|profit(?:\s+and\s+loss)?)\s*$", re.I), _h_pnl),
    (re.compile(r"^\s*postex(?:\s+summary)?\s*$", re.I), _h_postex),
    (re.compile(r"^\s*(?:my\s+)?reminders\s*$", re.I), _h_reminders),
    (re.compile(r"^\s*(?:store\s+)?overview\s*$", re.I), _h_overview),
    (re.compile(r"^\s*abandoned(?:\s+checkouts)?\s*$", re.I), _h_abandoned),
    (re.compile(r"^\s*expenses\s*$", re.I), _h_expenses),
    (re.compile(r"^\s*vendors\s*$", re.I), _h_vendors),
    (re.compile(r"^\s*employees\s*$", re.I), _h_employees),
    (re.compile(r"^\s*cash(?:\s+accounts)?\s*$", re.I), _h_cash),
    (re.compile(r"^\s*(?:ad\s+insights|ads?\s+performance)\s*$", re.I), _h_ad_insights),
]

_ACTION_PATTERNS = [
    (re.compile(r"^\s*(?:fulfill|ship)\s+order\s*#?\s*(\d+)(?:\s*,?\s*(?:tracking|trk)[:\s]+(\S+))?\s*$", re.I), _h_fulfill),
    (re.compile(r"^\s*cancel\s+order\s*#?\s*(\d+)\s*$", re.I), _h_cancel),
    (re.compile(r"^\s*refund\s+order\s*#?\s*(\d+)\s*$", re.I), _h_refund),
    (re.compile(r"^\s*(?:set|update)\s+price\s+(?:of\s+)?(.+?)\s+to\s+(?:rs\.?|pkr)?\s*([\d.]+)\s*$", re.I), _h_set_price),
]


async def try_handle(mcp_server, db, conversation_id, user_text):
    """Returns a reply string if this message was fully handled without any
    LLM call, else None (caller should fall through to Gemini/Claude)."""
    text = (user_text or "").strip()
    if not text:
        return None
    for pattern, handler in _READ_PATTERNS:
        match = pattern.match(text)
        if match:
            try:
                result = await handler(match, mcp_server, db)
            except Exception as e:
                return f"Tried to handle that without AI but hit an error: {e}"
            if result is not None:
                return result
    for pattern, handler in _ACTION_PATTERNS:
        match = pattern.match(text)
        if match:
            try:
                result = await handler(match, mcp_server, db, conversation_id)
            except Exception as e:
                return f"Tried to handle that without AI but hit an error: {e}"
            if result is not None:
                return result
    return None
