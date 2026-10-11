"""
Filament raw-material inventory: stock-level math (pure functions, easy to
test in isolation) plus the order-fulfillment hook that deducts grams
automatically as orders go out.

Ledger model: current stock for a filament type = SUM(its purchases' grams)
- SUM(its consumption's grams). Nothing is a mutable running total, so it's
always re-derivable and auditable, same convention as the rest of the
accounting module.

Matching an order line item to a filament type, in priority order:
  1. An exact (product_id, variant_id) row in product_filament_links --
     a manual override the user set (directly, or by "remember this" when
     resolving a past needs-review item).
  2. A product-level default (product_id, variant_id=NULL) row.
  3. Auto-match: the variant's colour (read off variant_title, e.g. "Black /
     Large" -> "Black") against a stocked filament type's `color`, using the
     store-wide default grams-per-unit. Only applied when exactly one active
     filament type matches that colour -- an ambiguous match (e.g. two
     materials both stocked in "Black") is treated as unresolved rather than
     guessing.
  4. Anything still unresolved (no colour on the variant at all, a multi-
     colour product, no spool stocked in that colour yet, or an ambiguous
     match) is logged as `needs_review` with no filament type attached, so
     it never silently mis-attributes consumption. It still gets a
     candidate gram amount recorded (qty x default grams-per-unit) as a
     starting point for when the user manually resolves it.
"""

from datetime import datetime, timedelta, timezone
from math import ceil


def _variant_color(line_item):
    # Shopify order line items don't carry selected-option names directly --
    # just `variant_title` (e.g. "Black / Large" for a multi-option variant,
    # or just "Black" for a colour-only variant). Taking the first "/"
    # segment is a reliable colour guess for how this store's variants are
    # named (colour first), without needing an extra API call per line item.
    vt = (line_item.get("variant_title") or "").strip()
    if not vt:
        return None
    return vt.split("/")[0].strip() or None


async def _default_grams_per_unit(db):
    try:
        settings = await db.get_cost_settings()
        return float(settings.get("default_grams_per_unit", 50) or 50)
    except Exception:
        return 50.0


async def match_filament_for_line_item(db, line_item):
    """Returns (filament_type_id_or_None, grams_per_unit, matched_label)."""
    product_id = line_item.get("product_id")
    variant_id = line_item.get("variant_id")
    default_grams = await _default_grams_per_unit(db)

    if product_id and variant_id:
        link = await db.get_product_filament_link(product_id, variant_id)
        if link:
            return link["filament_type_id"], float(link["grams_per_unit"]), "manual"
    if product_id:
        link = await db.get_product_filament_link(product_id, None)
        if link:
            return link["filament_type_id"], float(link["grams_per_unit"]), "manual"

    color_guess = _variant_color(line_item)
    if color_guess:
        types = await db.list_filament_types()
        matches = [t for t in types if t.get("active", True)
                   and (t.get("color") or "").strip().lower() == color_guess.lower()]
        if len(matches) == 1:
            return matches[0]["id"], default_grams, "auto"

    return None, default_grams, "needs_review"


async def deduct_filament_for_order(db, shopify, order_id):
    """Call right after an order is fulfilled -- idempotent per order (checks
    order_has_filament_consumption first), so a retried fulfillment call or a
    double-click never double-deducts stock. Both fulfill_order call sites
    (the dashboard REST route and the MCP/agent tool) call this."""
    try:
        if await db.order_has_filament_consumption(order_id):
            return {"ok": True, "skipped": True, "reason": "already logged for this order"}

        order_data = await shopify._get(f"/orders/{order_id}.json")
        o = order_data.get("order", order_data)
        line_items = o.get("line_items", [])

        logged = 0
        needs_review = 0
        for li in line_items:
            qty = int(li.get("quantity") or 1)
            filament_type_id, grams_per_unit, matched = await match_filament_for_line_item(db, li)
            await db.create_filament_consumption({
                "filament_type_id": filament_type_id,
                "grams": round(grams_per_unit * qty, 2),
                "shopify_order_id": order_id,
                "shopify_order_name": o.get("name", ""),
                "product_id": li.get("product_id"),
                "variant_id": li.get("variant_id"),
                "product_title": li.get("title", "") or "",
                "variant_title": li.get("variant_title", "") or "",
                "matched": matched,
            })
            logged += 1
            if matched == "needs_review":
                needs_review += 1
        return {"ok": True, "skipped": False, "logged": logged, "needs_review": needs_review}
    except Exception as e:
        # Filament tracking must never block or fail an actual order
        # fulfillment -- that's the real-world action that matters. Log and
        # move on; the order stays fulfilled either way.
        return {"ok": False, "error": str(e)}


def _parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def compute_filament_status(filament_types, purchases, consumption, lead_time_days=14, lookback_days=30):
    """Per filament type: current stock, recent daily usage velocity,
    projected days left, and a reorder suggestion (in whole spools) sized to
    cover the lookback-derived usage rate through the next lead_time_days
    plus a lookback_days buffer. Pure function -- no I/O -- so it's cheap to
    unit test against hand-built fixtures."""
    now = datetime.utcnow()
    lookback_cutoff = now - timedelta(days=lookback_days)

    purchased_by_type = {}
    for p in purchases:
        tid = p.get("filament_type_id")
        purchased_by_type[tid] = purchased_by_type.get(tid, 0) + float(p.get("grams") or 0)

    consumed_by_type = {}
    recent_consumed_by_type = {}
    for c in consumption:
        tid = c.get("filament_type_id")
        if not tid:
            continue  # needs_review rows aren't attributed to any type yet
        grams = float(c.get("grams") or 0)
        consumed_by_type[tid] = consumed_by_type.get(tid, 0) + grams
        created = _parse_dt(c.get("created_at"))
        if created and created >= lookback_cutoff:
            recent_consumed_by_type[tid] = recent_consumed_by_type.get(tid, 0) + grams

    results = []
    for t in filament_types:
        tid = t["id"]
        in_stock = purchased_by_type.get(tid, 0) - consumed_by_type.get(tid, 0)
        recent = recent_consumed_by_type.get(tid, 0)
        avg_daily = (recent / lookback_days) if lookback_days else 0
        days_left = (in_stock / avg_daily) if avg_daily > 0 else None
        reorder_threshold = float(t.get("reorder_threshold_grams") or 0)
        needs_reorder = in_stock <= reorder_threshold or (days_left is not None and days_left <= lead_time_days)
        spool_weight = float(t.get("spool_weight_grams") or 1000) or 1000

        suggested_spools = 0
        if needs_reorder:
            target_grams = avg_daily * (lead_time_days + lookback_days) if avg_daily > 0 else spool_weight
            needed = max(target_grams - in_stock, spool_weight)
            suggested_spools = max(1, ceil(needed / spool_weight))

        results.append({
            "id": tid, "material": t.get("material"), "color": t.get("color"),
            "in_stock_grams": round(in_stock, 1),
            "avg_daily_consumption_grams": round(avg_daily, 2),
            "projected_days_left": round(days_left, 1) if days_left is not None else None,
            "reorder_threshold_grams": reorder_threshold,
            "spool_weight_grams": spool_weight,
            "needs_reorder": bool(needs_reorder),
            "suggested_purchase_spools": suggested_spools,
            "suggested_purchase_grams": suggested_spools * spool_weight if needs_reorder else 0,
            "active": t.get("active", True),
        })

    results.sort(key=lambda r: (not r["needs_reorder"],
                                 r["projected_days_left"] if r["projected_days_left"] is not None else float("inf")))
    return results
