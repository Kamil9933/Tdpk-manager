"""
Builds the plain-text digest sent over Telegram/email (core/notify.py) on a
schedule (see the /api/cron/tick route in main.py). Kept as plain text, not
HTML/markdown, so it renders cleanly in both a Telegram message and a plain
email body with no extra formatting work.

Every section is independent: one failing (PostEx down, SEO tables not
created yet) never stops the rest of the digest.
"""

from datetime import datetime, timedelta, timezone
from core.filament import compute_filament_status
from core.gemini_fallback import redact
from core import risk

PKT = timezone(timedelta(hours=5))

# Bambu's access token lasts ~90 days (community-documented, Bambu publishes
# nothing). Printer status and queue sync silently stop when it expires, so
# warn ahead of time. Re-login: dashboard -> printers -> Bambu login.
BAMBU_TOKEN_DAYS = 90
BAMBU_WARN_DAYS = 80
STUCK_PARCEL_DAYS = 7
HISTORY_DAYS = 90


def _parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        try:  # PostEx: "2026-10-10T18:58:44.000+0500"
            dt = datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%S.%f%z")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def stuck_parcels(postex_orders, now=None, days=STUCK_PARCEL_DAYS):
    """In-transit PostEx parcels booked more than `days` ago."""
    now = now or datetime.now(timezone.utc)
    out = []
    for o in postex_orders:
        if o.get("_bucket") != "in_transit":
            continue
        booked = _parse(o.get("transactionDate"))
        if booked and now - booked > timedelta(days=days):
            out.append({"ref": o.get("orderRefNumber") or o.get("trackingNumber"),
                        "tracking": o.get("trackingNumber"), "city": o.get("cityName"),
                        "status": o.get("transactionStatus"), "days": (now - booked).days})
    return sorted(out, key=lambda p: -p["days"])


def bambu_token_warning(settings, has_printers, now=None):
    if not has_printers:
        return None
    if not settings.get("bambu_access_token"):
        return "Bambu printers aren't connected (no login token) -- printer status and queue sync are off."
    got = _parse(settings.get("bambu_token_obtained_at"))
    if not got:
        return None
    age = ((now or datetime.now(timezone.utc)) - got).days
    if age >= BAMBU_TOKEN_DAYS:
        return f"Bambu login is {age} days old and has probably expired -- log in again from the dashboard."
    if age >= BAMBU_WARN_DAYS:
        return f"Bambu login expires in about {BAMBU_TOKEN_DAYS - age} day(s) -- log in again from the dashboard soon."
    return None


async def generate_digest(db, shopify, postex):
    lines = []
    today = datetime.now(PKT).date()

    lines.append(f"TDPK Manager -- digest for {today.isoformat()}")
    lines.append("")

    # Shopify orders in roughly the last 24h
    try:
        orders = await shopify.get_all_orders(status="any", days=1)
        revenue = sum(float(o.get("total_price", 0) or 0) for o in orders)
        lines.append(f"Shopify: {len(orders)} order(s) in the last 24h, ~PKR {revenue:,.0f} total.")
    except Exception as e:
        lines.append(redact(f"Shopify: could not load ({e})."))

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
        lines.append(redact(f"PostEx: could not load ({e})."))

    # 90 days of PostEx outcomes: feeds both the stuck-parcel list and the COD risk check
    history_orders = []
    try:
        history_orders, _ = await postex.get_orders_classified(
            status_id=0, from_date=(today - timedelta(days=HISTORY_DAYS)).isoformat(), to_date=today.isoformat())
    except Exception:
        pass

    stuck = stuck_parcels(history_orders)
    if stuck:
        shown = "; ".join(f"{p['ref']} ({p['city']}, {p['status']}, {p['days']}d)" for p in stuck[:8])
        lines.append(f"Parcels in transit over {STUCK_PARCEL_DAYS} days ({len(stuck)}): {shown}"
                     + (" ..." if len(stuck) > 8 else ""))

    # COD risk: unfulfilled orders worth a call before booking
    try:
        if history_orders:
            history = risk.build_history(history_orders)
            open_orders = risk.awaiting_booking(await shopify.get_orders(status="open"))
            flagged = [a for a in (risk.assess(o, history) for o in open_orders) if a["level"] != "low"]
            if flagged:
                lines.append(f"Call before booking ({len(flagged)}):")
                lines += [f"  - {line}" for line in risk.format_flags(flagged)[:10]]
    except Exception as e:
        lines.append(redact(f"COD risk check: could not run ({e})."))

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

    # SEO (written by the GitHub Actions SEO report)
    try:
        run = await db.latest_seo_run()
        drafts = await db.count_seo_suggestions(status="draft")
        if run:
            lines.append(f"SEO: last report {str(run.get('started_at'))[:16].replace('T', ' ')} UTC ({run.get('status')}), "
                         f"{drafts} draft suggestion(s) waiting for approval.")
    except Exception:
        pass

    # Bambu login expiry
    try:
        settings = await db.get_cost_settings()
        printers = [p for p in await db.list_printers() if p.get("bambu_dev_id")]
        warning = bambu_token_warning(settings, bool(printers))
        if warning:
            lines.append(warning)
    except Exception:
        pass

    return "\n".join(lines)
