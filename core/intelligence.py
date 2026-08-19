def generate_briefing(products, orders_today, orders_30d, cost_config):
    total = len(products)
    active = sum(1 for p in products if p.get("status")=="active")
    draft = sum(1 for p in products if p.get("status")=="draft")
    zero_price = sum(1 for p in products if all(float(v.get("price",0))==0 for v in p.get("variants",[])))
    no_image = sum(1 for p in products if not p.get("images"))
    out_of_stock = sum(1 for p in products if sum(v.get("inventory_quantity",0) for v in p.get("variants",[]))<=0 and p.get("status")=="active")
    no_tags = sum(1 for p in products if not p.get("tags","").strip())

    revenue_30 = sum(float(o.get("total_price","0")) for o in orders_30d)
    orders_count = len(orders_30d)
    revenue_today = sum(float(o.get("total_price","0")) for o in orders_today)
    orders_today_count = len(orders_today)

    total_cost_per_order = (
        cost_config["avg_filament_per_order"] +
        cost_config["avg_packaging_shipping"] +
        cost_config["ad_spend_per_order"] +
        cost_config["fixed_monthly"] / max(orders_count, 1)
    )
    avg_order_value = revenue_30 / max(orders_count, 1)
    est_profit_per_order = avg_order_value - total_cost_per_order
    profit_margin = (est_profit_per_order / avg_order_value * 100) if avg_order_value > 0 else 0

    insights = []
    actions = []

    if zero_price > 0:
        insights.append({"type":"danger","text":f"{zero_price} products have PKR 0 price — you cannot sell these"})
        actions.append({"label":f"Fix {zero_price} zero-price products","filter":"zero_price"})
    if out_of_stock > 0:
        insights.append({"type":"warning","text":f"{out_of_stock} active products are out of stock"})
        actions.append({"label":"Enable continue-selling on out of stock","op":"continue_selling"})
    if draft > 50:
        insights.append({"type":"info","text":f"{draft} products are still in draft — potential revenue sitting unpublished"})
        actions.append({"label":f"Review & publish drafts","filter":"status_draft"})
    if no_image > 0:
        insights.append({"type":"warning","text":f"{no_image} products have no images — these won't sell well"})
    if profit_margin < 20 and orders_count > 0:
        insights.append({"type":"danger","text":f"Estimated margin is {profit_margin:.0f}% — below healthy threshold of 35%"})
    if orders_count > 0 and avg_order_value < 1955:
        insights.append({"type":"warning","text":f"Avg order PKR {avg_order_value:.0f} is below your break-even of PKR 1,955"})
    if orders_today_count > 0:
        insights.append({"type":"success","text":f"{orders_today_count} orders today worth PKR {revenue_today:,.0f}"})

    return {
        "snapshot": {
            "total_products": total, "active": active, "draft": draft,
            "zero_price": zero_price, "no_image": no_image, "out_of_stock": out_of_stock,
            "no_tags": no_tags,
        },
        "orders": {
            "today": orders_today_count, "revenue_today": round(revenue_today, 0),
            "last_30d": orders_count, "revenue_30d": round(revenue_30, 0),
            "avg_order": round(avg_order_value, 0),
        },
        "financials": {
            "cost_per_order": round(total_cost_per_order, 0),
            "est_profit_per_order": round(est_profit_per_order, 0),
            "margin_pct": round(profit_margin, 1),
            "monthly_revenue": round(revenue_30, 0),
            "target_orders": cost_config["target_monthly_orders"],
        },
        "insights": insights,
        "quick_actions": actions,
    }

def analyze_pricing(product, cost_config):
    prices = [float(v.get("price",0)) for v in product.get("variants",[]) if float(v.get("price",0))>0]
    if not prices:
        return {"status":"red","recommendation":"Set a price for this product"}
    avg = sum(prices)/len(prices)
    overhead = cost_config["fixed_monthly"] / cost_config["target_monthly_orders"]
    total_cost = cost_config["avg_filament_per_order"] + cost_config["avg_packaging_shipping"] + cost_config["ad_spend_per_order"] + overhead
    margin = ((avg - total_cost) / avg * 100) if avg > 0 else -100
    breakeven = total_cost
    recommended = round(total_cost / 0.65)  # 35% margin target
    if margin >= 50:
        return {"status":"blue","recommendation":f"Excellent margin. Consider A/B testing higher price.","breakeven":breakeven,"recommended":recommended,"current":avg,"margin":margin}
    elif margin >= 35:
        return {"status":"green","recommendation":"Good pricing. Healthy margin.","breakeven":breakeven,"recommended":recommended,"current":avg,"margin":margin}
    elif margin >= 20:
        return {"status":"yellow","recommendation":f"Thin margin. Consider raising to PKR {recommended:,.0f}","breakeven":breakeven,"recommended":recommended,"current":avg,"margin":margin}
    elif margin >= 0:
        return {"status":"orange","recommendation":f"Very thin. Raise to at least PKR {recommended:,.0f} to hit 35% margin","breakeven":breakeven,"recommended":recommended,"current":avg,"margin":margin}
    else:
        return {"status":"red","recommendation":f"Selling at a loss. Minimum viable price: PKR {int(breakeven)+1:,}","breakeven":breakeven,"recommended":recommended,"current":avg,"margin":margin}
