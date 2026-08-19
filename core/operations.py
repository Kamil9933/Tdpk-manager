import asyncio
from typing import List, Dict, Any

# Shared progress store — operation_id → progress dict
progress_store: Dict[str, Any] = {}

def _find_color_option_pos(product):
    for opt in product.get("options", []):
        if opt["name"].lower() == "color":
            return opt["position"], opt["id"]
    return None, None

async def run_operation(operation: str, products: List[dict], params: dict, shopify) -> dict:
    ops = {
        "remove_color": op_remove_color,
        "add_color": op_add_color,
        "rename_color": op_rename_color,
        "price_percentage": op_price_percentage,
        "price_fixed": op_price_fixed,
        "price_compare_at": op_price_compare_at,
        "price_remove_sale": op_price_remove_sale,
        "set_stock": op_set_stock,
        "inventory_tracking": op_inventory_tracking,
        "continue_selling": op_continue_selling,
        "disable_tax": op_disable_tax,
        "enable_tax": op_enable_tax,
        "add_tag": op_add_tag,
        "remove_tag": op_remove_tag,
        "publish": op_publish,
        "unpublish": op_unpublish,
        "set_vendor": op_set_vendor,
        "set_product_type": op_set_product_type,
        "set_weight": op_set_weight,
    }

    handler = ops.get(operation)
    if not handler:
        return {"error": f"Unknown operation: {operation}"}

    return await handler(products, params, shopify)

async def _progress(log_id, done, total, message="", finished=False):
    progress_store[log_id] = {
        "done": finished,
        "processed": done,
        "total": total,
        "pct": round((done / total) * 100) if total else 100,
        "message": message
    }

# ── Colour operations ──────────────────────────────────────────────────────────

async def op_remove_color(products, params, shopify):
    color = params.get("color", "").lower()
    changed = 0
    errors = 0
    total = len(products)

    for i, p in enumerate(products):
        pos, _ = _find_color_option_pos(p)
        if not pos:
            continue
        pos_key = f"option{pos}"
        to_delete = [v for v in p.get("variants", [])
                     if v.get(pos_key, "").lower() == color]
        for v in to_delete:
            try:
                await shopify.delete_variant(p["id"], v["id"])
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)

    return {"changed": changed, "errors": errors, "total_products": total}

async def op_add_color(products, params, shopify):
    color = params.get("color")
    price = params.get("price")
    qty = int(params.get("qty", 100))
    changed = 0
    skipped = 0
    errors = 0

    for p in products:
        pos, _ = _find_color_option_pos(p)
        if pos:
            pos_key = f"option{pos}"
            if any(v.get(pos_key, "").lower() == color.lower()
                   for v in p.get("variants", [])):
                skipped += 1
                continue

        variant_price = price or (p["variants"][0]["price"] if p.get("variants") else "0.00")
        try:
            await shopify.add_variant(p["id"], {
                "option1": color,
                "price": str(variant_price),
                "inventory_quantity": qty,
                "inventory_management": "shopify",
            })
            changed += 1
        except Exception:
            errors += 1
        await asyncio.sleep(0.3)

    return {"changed": changed, "skipped": skipped, "errors": errors}

async def op_rename_color(products, params, shopify):
    old = params.get("old_color", "").lower()
    new = params.get("new_color")
    changed = 0
    errors = 0

    for p in products:
        pos, _ = _find_color_option_pos(p)
        if not pos:
            continue
        pos_key = f"option{pos}"
        for v in p.get("variants", []):
            if v.get(pos_key, "").lower() == old:
                try:
                    await shopify.update_variant(p["id"], v["id"], {pos_key: new})
                    changed += 1
                except Exception:
                    errors += 1
        await asyncio.sleep(0.3)

    return {"changed": changed, "errors": errors}

# ── Price operations ───────────────────────────────────────────────────────────

async def op_price_percentage(products, params, shopify):
    pct = float(params.get("percentage", 0))
    factor = 1 + (pct / 100)
    changed = 0
    errors = 0

    for p in products:
        for v in p.get("variants", []):
            new_price = round(float(v.get("price", 0)) * factor, 2)
            try:
                await shopify.update_variant(p["id"], v["id"], {"price": f"{new_price:.2f}"})
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)

    return {"changed": changed, "errors": errors}

async def op_price_fixed(products, params, shopify):
    price = str(params.get("price", "0.00"))
    changed = 0
    errors = 0

    for p in products:
        for v in p.get("variants", []):
            try:
                await shopify.update_variant(p["id"], v["id"], {"price": price})
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)

    return {"changed": changed, "errors": errors}

async def op_price_compare_at(products, params, shopify):
    multiplier = float(params.get("multiplier", 1.3))
    changed = 0
    errors = 0

    for p in products:
        for v in p.get("variants", []):
            orig = round(float(v.get("price", 0)) * multiplier, 2)
            try:
                await shopify.update_variant(p["id"], v["id"],
                                             {"compare_at_price": f"{orig:.2f}"})
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)

    return {"changed": changed, "errors": errors}

async def op_price_remove_sale(products, params, shopify):
    changed = 0
    errors = 0

    for p in products:
        for v in p.get("variants", []):
            try:
                await shopify.update_variant(p["id"], v["id"], {"compare_at_price": None})
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)

    return {"changed": changed, "errors": errors}

# ── Inventory operations ───────────────────────────────────────────────────────

async def op_set_stock(products, params, shopify):
    qty = int(params.get("qty", 100))
    location_id = params.get("location_id")
    color_filter = params.get("color_filter", "").lower()
    changed = 0
    errors = 0

    for p in products:
        pos, _ = _find_color_option_pos(p)
        pos_key = f"option{pos}" if pos else None

        for v in p.get("variants", []):
            if color_filter and pos_key:
                if v.get(pos_key, "").lower() != color_filter:
                    continue
            inv_item_id = v.get("inventory_item_id")
            if not inv_item_id:
                continue
            try:
                level = await shopify.get_inventory_level(inv_item_id, location_id)
                current = level.get("available", 0) if level else 0
                adjustment = qty - current
                await shopify.adjust_inventory(location_id, inv_item_id, adjustment)
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)

    return {"changed": changed, "errors": errors}

async def op_inventory_tracking(products, params, shopify):
    enable = params.get("enable", True)
    mgmt = "shopify" if enable else ""
    changed = 0
    errors = 0

    for p in products:
        for v in p.get("variants", []):
            try:
                await shopify.update_variant(p["id"], v["id"],
                                             {"inventory_management": mgmt})
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)

    return {"changed": changed, "errors": errors}

async def op_continue_selling(products, params, shopify):
    changed = 0
    errors = 0

    for p in products:
        for v in p.get("variants", []):
            try:
                await shopify.update_variant(p["id"], v["id"],
                                             {"inventory_policy": "continue"})
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)

    return {"changed": changed, "errors": errors}

# ── Tax operations ─────────────────────────────────────────────────────────────

async def op_disable_tax(products, params, shopify):
    changed = 0
    errors = 0
    for p in products:
        for v in p.get("variants", []):
            try:
                await shopify.update_variant(p["id"], v["id"], {"taxable": False})
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)
    return {"changed": changed, "errors": errors}

async def op_enable_tax(products, params, shopify):
    changed = 0
    errors = 0
    for p in products:
        for v in p.get("variants", []):
            try:
                await shopify.update_variant(p["id"], v["id"], {"taxable": True})
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)
    return {"changed": changed, "errors": errors}

# ── Tag operations ─────────────────────────────────────────────────────────────

async def op_add_tag(products, params, shopify):
    tag = params.get("tag", "").strip()
    changed = 0
    errors = 0

    for p in products:
        existing = [t.strip() for t in p.get("tags", "").split(",") if t.strip()]
        if tag not in existing:
            existing.append(tag)
            try:
                await shopify.update_product(p["id"], {"tags": ", ".join(existing)})
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)

    return {"changed": changed, "errors": errors}

async def op_remove_tag(products, params, shopify):
    tag = params.get("tag", "").strip()
    changed = 0
    errors = 0

    for p in products:
        existing = [t.strip() for t in p.get("tags", "").split(",") if t.strip()]
        if tag in existing:
            existing.remove(tag)
            try:
                await shopify.update_product(p["id"], {"tags": ", ".join(existing)})
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)

    return {"changed": changed, "errors": errors}

# ── Status operations ──────────────────────────────────────────────────────────

async def op_publish(products, params, shopify):
    changed = 0
    errors = 0
    for p in products:
        try:
            await shopify.update_product(p["id"], {"status": "active"})
            changed += 1
        except Exception:
            errors += 1
        await asyncio.sleep(0.3)
    return {"changed": changed, "errors": errors}

async def op_unpublish(products, params, shopify):
    changed = 0
    errors = 0
    for p in products:
        try:
            await shopify.update_product(p["id"], {"status": "draft"})
            changed += 1
        except Exception:
            errors += 1
        await asyncio.sleep(0.3)
    return {"changed": changed, "errors": errors}

# ── Product field operations ───────────────────────────────────────────────────

async def op_set_vendor(products, params, shopify):
    vendor = params.get("vendor", "")
    changed = 0
    errors = 0
    for p in products:
        try:
            await shopify.update_product(p["id"], {"vendor": vendor})
            changed += 1
        except Exception:
            errors += 1
        await asyncio.sleep(0.3)
    return {"changed": changed, "errors": errors}

async def op_set_product_type(products, params, shopify):
    ptype = params.get("product_type", "")
    changed = 0
    errors = 0
    for p in products:
        try:
            await shopify.update_product(p["id"], {"product_type": ptype})
            changed += 1
        except Exception:
            errors += 1
        await asyncio.sleep(0.3)
    return {"changed": changed, "errors": errors}

async def op_set_weight(products, params, shopify):
    weight = float(params.get("weight", 0))
    unit = params.get("unit", "g")
    changed = 0
    errors = 0
    for p in products:
        for v in p.get("variants", []):
            try:
                await shopify.update_variant(p["id"], v["id"],
                                             {"weight": weight, "weight_unit": unit})
                changed += 1
            except Exception:
                errors += 1
        await asyncio.sleep(0.3)
    return {"changed": changed, "errors": errors}
