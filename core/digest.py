"""
Builds the plain-text digest sent over Telegram/email (core/notify.py) on a
schedule (see the /api/cron/tick route in main.py). Kept as plain text, not
HTML/markdown, so it renders cleanly in both a Telegram message and a plain
email body with no extra formatting work.
"""

from datetime import datetime, timedelta
from core.filament import compute_filament_status


async def generate_digest(db, shopify, postex):
    lines = []
    now = datetime.utcnow()
    today = now.date().isoformat()
    yesterday = (now - timedelta(days=1)).date().isoformat()

    lines.append(f"TDPK Manager -- digest for {today}")
    lines.append("")

    # Shopify orders in roughly the last 24h
    try:
        orders = await shopify.get_all_orders_range(yesterday, today, status="any")
        revenue = sum(float(o.get("total_price", 0) or 0) for o in orders)
        lines.append(f"Shopify: {len(orders)} order(s) in the last 24h, ~PKR {revenue:,.0f} total.")
    except Exception as e:
        lines.append(f"Shopify: could not load ({e}).")

    # PostEx summary -- 7 days, for a stable return-rate read rather than a noisy 1-day one
    try:
        summary = await postex.get_dashboard_summary(days=7)
        lines.append(
            f"PostEx (7d): {summary['total_orders']} orders -- "
            f"{summary['delivered']} delivered ({summary['delivery_rate']}%), "
            f"{summary['returned']} returned ({summary['return_rate']}%), "
            f"{summary['in_transit']} in transit. Pending COD: PKR {summary['pending_cod']:,.0f}."
        )
    except Exception as e:
        lines.append(f"PostEx: could not load ({e}).")

    # Low Shopify stock (finished goods)
    try:
        products = await shopify.get_all_products()
        low = []
        for p in products:
            for v in p.get("variants", []):
                qty = v.get("inventory_quantity")
                if qty is not None and qty <= 5:
                    low.append(f"{p.get('title')} ({v.get('title')}): {qty} left")
        if low:
            shown = "; ".join(low[:8]) + (" ..." if len(low) > 8 else "")
            lines.append(f"Low stock ({len(low)} variant(s)): {shown}")
    except Exception:
        pass

    # Filament (raw material)
    try:
        types = await db.list_filament_types()
        if types:
            purchases = await db.list_filament_purchases()
            consumption = await db.list_filament_consumption(days=60)
            status = compute_filament_status(types, purchases, consumption)
            reorder = [s for s in status if s["needs_reorder"] and s["active"]]
            if reorder:
                parts = [
                    f"{s['material']} {s['color']} ({s['in_stock_grams']:.0f}g left"
                    + (f", ~{s['projected_days_left']:.0f}d" if s["projected_days_left"] is not None else "")
                    + f") -> buy {s['suggested_purchase_spools']} spool(s)"
                    for s in reorder
                ]
                lines.append("Filament to reorder: " + "; ".join(parts))
            needs_review = await db.list_filament_needs_review()
            if needs_review:
                lines.append(f"{len(needs_review)} order line item(s) need manual filament review in the dashboard.")
    except Exception:
        pass

    return "\n".join(lines)
