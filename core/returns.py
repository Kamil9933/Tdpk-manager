"""
Cross-references PostEx order outcomes (delivered/returned/in transit) with
Shopify order line items via the orderRefNumber PostEx stores on each order
(the reference the Shopify-PostEx booking integration set when the shipment
was created) -- giving per-product/variant return rates, something PostEx's
own data can't answer on its own since it has no idea what was inside the
box (see the docstring on get_postex_return_value_breakdown in
core/mcp_server.py, which called this out as a known gap).

Matching is best-effort by design: it normalizes both sides' reference
numbers (strips '#', non-digit characters, leading zeros) and tries a
couple of Shopify order fields (name, order_number) since real-world PostEx
integrations aren't always consistent about what they put in
orderRefNumber. Orders that can't be matched are reported separately
(match_rate) rather than silently dropped, so this is never presented as
more complete than it actually is.
"""

import re


def _normalize_ref(value):
    if value is None:
        return None
    s = str(value).strip().lstrip("#")
    s = re.sub(r"\D", "", s)  # order numbers are numeric -- keep digits only
    if not s:
        return None
    return s.lstrip("0") or "0"


def build_shopify_order_index(shopify_orders):
    """Indexes Shopify orders by every normalized reference they could be
    found under (name, order_number, id), so a PostEx orderRefNumber in any
    of those shapes resolves to the right order."""
    index = {}
    for o in shopify_orders:
        for raw in (o.get("name"), o.get("order_number"), o.get("id")):
            key = _normalize_ref(raw)
            if key and key not in index:
                index[key] = o
    return index


def compute_product_return_breakdown(postex_orders, shopify_orders):
    order_index = build_shopify_order_index(shopify_orders)
    matched = 0
    unmatched = 0
    by_product = {}

    for po in postex_orders:
        ref = _normalize_ref(po.get("orderRefNumber"))
        so = order_index.get(ref) if ref else None
        if not so:
            unmatched += 1
            continue
        matched += 1
        bucket = po.get("_bucket")
        for li in so.get("line_items", []):
            key = (li.get("product_id"), li.get("variant_id"))
            row = by_product.setdefault(key, {
                "product_id": li.get("product_id"),
                "variant_id": li.get("variant_id"),
                "title": li.get("title", "") or "",
                "variant_title": li.get("variant_title", "") or "",
                "total": 0, "returned": 0, "delivered": 0, "in_transit": 0,
            })
            row["total"] += 1
            if bucket == "returned":
                row["returned"] += 1
            elif bucket == "delivered":
                row["delivered"] += 1
            elif bucket == "in_transit":
                row["in_transit"] += 1

    results = []
    for row in by_product.values():
        row["return_rate"] = round(row["returned"] / row["total"] * 100, 1) if row["total"] else 0
        results.append(row)
    results.sort(key=lambda r: (r["return_rate"], r["total"]), reverse=True)

    total_postex = len(postex_orders)
    return {
        "products": results,
        "matched_orders": matched,
        "unmatched_orders": unmatched,
        "match_rate": round(matched / total_postex * 100, 1) if total_postex else 0,
    }
