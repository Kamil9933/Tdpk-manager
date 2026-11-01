"""
Cash-on-delivery risk check for new Shopify orders -- "should I call this
customer before booking the parcel?". A refused COD parcel costs a courier
fee both ways and ties up stock for a week or more, so a 30-second call on
the risky ones is worth it.

Built only from data this app already has: PostEx's own history (who
refused before, which cities refuse more) plus the Shopify order itself.
Scoring is deliberately simple and explainable -- every point comes with a
plain reason shown to the human, never a hidden model:

  +3  this phone number refused (returned) a PostEx parcel before
  -1  ...or has had parcels delivered before, with no refusals
  +2  the city's return rate (last 90 days, 5+ parcels) is at least double
      the store average and at least 15%
  +1  order total Rs 5,000+   (+2 at Rs 10,000+)
  +1  first order from this customer AND Rs 3,000+

  high = 3+, medium = 2, low = below 2.
"""
import re

HIGH, MEDIUM = 3, 2
CITY_MIN_ORDERS = 5
CITY_MIN_RATE = 15.0


def normalize_phone(raw):
    """Pakistani numbers arrive as +92 300..., 0300-..., 92300...; reduce to
    the last 10 digits so the same SIM matches across Shopify and PostEx."""
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[-10:] if len(digits) >= 10 else None


def build_history(postex_orders):
    """Per-phone and per-city outcome counts from classified PostEx orders
    (PostexClient.get_orders_classified)."""
    phones, cities = {}, {}
    total = returned = 0
    for o in postex_orders:
        bucket = o.get("_bucket")
        if bucket not in ("delivered", "returned"):
            continue  # still moving -- no outcome yet
        total += 1
        returned += bucket == "returned"
        phone = normalize_phone(o.get("customerPhone"))
        if phone:
            p = phones.setdefault(phone, {"delivered": 0, "returned": 0})
            p[bucket] += 1
        city = (o.get("cityName") or "").strip().lower()
        if city:
            c = cities.setdefault(city, {"total": 0, "returned": 0})
            c["total"] += 1
            c["returned"] += bucket == "returned"
    avg = (returned / total * 100) if total else 0.0
    return {"phones": phones, "cities": cities, "avg_return_rate": round(avg, 1)}


def order_phone(order):
    return (order.get("phone") or (order.get("shipping_address") or {}).get("phone")
            or (order.get("customer") or {}).get("phone"))


def assess(order, history):
    score, reasons = 0, []
    phone = normalize_phone(order_phone(order))
    past = history["phones"].get(phone) if phone else None
    if past and past["returned"]:
        score += 3
        reasons.append(f"this number refused {past['returned']} earlier parcel(s)")
    elif past and past["delivered"]:
        score -= 1
        reasons.append(f"this number accepted {past['delivered']} earlier parcel(s)")

    city = ((order.get("shipping_address") or {}).get("city") or "").strip()
    c = history["cities"].get(city.lower())
    if c and c["total"] >= CITY_MIN_ORDERS:
        rate = c["returned"] / c["total"] * 100
        if rate >= max(2 * history["avg_return_rate"], CITY_MIN_RATE):
            score += 2
            reasons.append(f"{city} return rate {rate:.0f}% vs store {history['avg_return_rate']:.0f}%")

    total = float(order.get("total_price") or 0)
    if total >= 10000:
        score += 2
        reasons.append(f"high value Rs {total:,.0f}")
    elif total >= 5000:
        score += 1
        reasons.append(f"high value Rs {total:,.0f}")

    orders_count = (order.get("customer") or {}).get("orders_count")
    if orders_count is not None and int(orders_count) <= 1 and total >= 3000 and not past:
        score += 1
        reasons.append("first order from this customer")

    level = "high" if score >= HIGH else "medium" if score >= MEDIUM else "low"
    return {"order_id": order.get("id"), "order_name": order.get("name"), "total": total,
            "city": city, "score": score, "level": level, "reasons": reasons}


def awaiting_booking(orders):
    """Open, unfulfilled, uncancelled orders -- the ones still worth a call."""
    return [o for o in orders if not o.get("cancelled_at")
            and o.get("fulfillment_status") in (None, "", "unfulfilled")]


def format_flags(assessments):
    lines = []
    for a in sorted(assessments, key=lambda a: -a["score"]):
        lines.append(f"{a['order_name']} ({a['city'] or '?'}, Rs {a['total']:,.0f}) -- {a['level'].upper()}: "
                     + "; ".join(a["reasons"]))
    return lines
