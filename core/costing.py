"""
Per-product cost and margin from REAL filament usage, replacing the flat
"Rs 400 of filament per order" guess wherever a product's grams are known.

Grams per unit live in product_filament_links (the same table filament
deduction already uses), set either by resolving a needs-review item with
"remember for product" or by the set_product_filament_usage tool -- e.g.
after slicing the model with the tdpk-print-pricing skill. Filament cost per
gram is the average over every logged filament purchase (Rs 4.50/g when this
was written: Rs 49,489 for 11 kg).

Everything else (packaging/shipping, ad spend per order, fixed overhead) is
still the flat per-order estimate from COST_CONFIG below -- only the
filament part becomes real.
"""

FALLBACK_COST_PER_GRAM = 4.5

# Flat per-order estimates (PKR), shared by the dashboard, briefing and MCP tools.
COST_CONFIG = {
    "avg_filament_per_order": 400,
    "avg_packaging_shipping": 400,
    "ad_spend_per_order": 600,
    "fixed_monthly": 55500,
    "target_monthly_orders": 300,
}


def filament_cost_per_gram(purchases):
    grams = sum(float(p.get("grams") or 0) for p in purchases if float(p.get("cost") or 0) > 0)
    cost = sum(float(p.get("cost") or 0) for p in purchases if float(p.get("cost") or 0) > 0)
    return round(cost / grams, 3) if grams else FALLBACK_COST_PER_GRAM


def links_index(links):
    return {(int(l["product_id"]), int(l["variant_id"]) if l.get("variant_id") is not None else None): float(l["grams_per_unit"])
            for l in links if l.get("product_id") is not None and l.get("grams_per_unit") is not None}


def grams_for(index, product_id, variant_id):
    """Variant-specific grams first, then the product-wide default."""
    if (product_id, variant_id) in index:
        return index[(product_id, variant_id)]
    return index.get((product_id, None))


def other_costs_per_order(cost_config):
    return (cost_config["avg_packaging_shipping"] + cost_config["ad_spend_per_order"]
            + cost_config["fixed_monthly"] / cost_config["target_monthly_orders"])


def variant_margins(product, index, cost_per_gram, cost_config):
    rows = []
    for v in product.get("variants", []):
        price = float(v.get("price") or 0)
        grams = grams_for(index, product.get("id"), v.get("id"))
        filament = grams * cost_per_gram if grams is not None else cost_config["avg_filament_per_order"]
        cost = filament + other_costs_per_order(cost_config)
        rows.append({
            "variant_id": v.get("id"), "variant": v.get("title"), "price": price,
            "grams": grams, "filament_cost": round(filament, 0), "total_cost": round(cost, 0),
            "margin_pct": round((price - cost) / price * 100, 1) if price else None,
            "basis": "real grams" if grams is not None else "estimate",
        })
    return rows
