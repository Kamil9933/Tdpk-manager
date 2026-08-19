import os, json, asyncio, hashlib, csv, io
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

from core.auth import verify_session, create_session, delete_session
from core.shopify import ShopifyClient
from core.supabase_client import db
from core.operations import run_operation
from core.intelligence import generate_briefing
from core.postex import PostexClient
from core.accounting import generate_pnl, straight_line_depreciation, calc_cash_balance, reconcile_postex

app = FastAPI(title="TD Shopify Manager v3")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

shopify = ShopifyClient(
    store=os.getenv("SHOPIFY_STORE", "1gdih3-zv.myshopify.com"),
    token=os.getenv("SHOPIFY_TOKEN", ""),
    client_id=os.getenv("SHOPIFY_CLIENT_ID", ""),
    client_secret=os.getenv("SHOPIFY_CLIENT_SECRET", "")
)
postex = PostexClient(token=os.getenv("POSTEX_TOKEN", ""))

COST_CONFIG = {
    "avg_filament_per_order": 400,
    "avg_packaging_shipping": 400,
    "ad_spend_per_order": 600,
    "fixed_monthly": 55500,
    "target_monthly_orders": 300,
}

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.0"}

# ── Auth ──────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    token = request.cookies.get("session")
    if token and await verify_session(request, return_bool=True):
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/api/auth/login")
async def login(request: Request):
    data = await request.json()
    from core.auth import verify_pin, verify_totp
    if not verify_pin(data.get("pin", "")):
        raise HTTPException(status_code=401, detail="Invalid PIN")
    if not verify_totp(data.get("totp", "")):
        raise HTTPException(status_code=401, detail="Invalid 2FA code")
    token = await create_session()
    response = JSONResponse({"ok": True})
    response.set_cookie("session", token, httponly=True, secure=True, samesite="strict", max_age=60*60*8)
    return response

@app.post("/api/auth/logout")
async def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        await delete_session(token)
    r = JSONResponse({"ok": True})
    r.delete_cookie("session")
    return r

@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if os.getenv("PIN_HASH"):
        return RedirectResponse("/")
    return templates.TemplateResponse("setup.html", {"request": request})

@app.post("/api/setup")
async def setup(request: Request):
    if os.getenv("PIN_HASH"):
        raise HTTPException(status_code=403, detail="Already configured")
    data = await request.json()
    pin = data.get("pin", "")
    if not pin.isdigit() or not (4 <= len(pin) <= 6):
        raise HTTPException(status_code=400, detail="PIN must be 4 to 6 digits")
    from core.auth import hash_pin, generate_totp_secret
    return {"pin_hash": hash_pin(pin), "totp_secret": generate_totp_secret()}

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, session=Depends(verify_session)):
    return templates.TemplateResponse("dashboard.html", {"request": request})

# ── Products ──────────────────────────────────────────────────────────────────
@app.get("/api/products")
async def get_products(
    page: int = 1, per_page: int = 50,
    collection_id: str = None, tag: str = None,
    status: str = None, search: str = None,
    min_price: float = None, max_price: float = None,
    vendor: str = None, product_type: str = None,
    sort_by: str = "title", sort_dir: str = "asc",
    no_image: bool = False, zero_price: bool = False,
    zero_stock: bool = False, no_tags: bool = False,
    session=Depends(verify_session)
):
    products = await shopify.get_all_products(collection_id=collection_id, tag=tag, status=status)
    if search:
        s = search.lower()
        products = [p for p in products if s in p.get("title","").lower() or s in p.get("vendor","").lower()]
    if vendor:
        products = [p for p in products if p.get("vendor","").lower() == vendor.lower()]
    if product_type:
        products = [p for p in products if p.get("product_type","").lower() == product_type.lower()]
    if min_price is not None or max_price is not None:
        products = [p for p in products if _price_in_range(p, min_price, max_price)]
    if no_image:
        products = [p for p in products if not p.get("images")]
    if zero_price:
        products = [p for p in products if all(float(v.get("price",0))==0 for v in p.get("variants",[]))]
    if zero_stock:
        products = [p for p in products if sum(v.get("inventory_quantity",0) for v in p.get("variants",[]))<=0]
    if no_tags:
        products = [p for p in products if not p.get("tags","").strip()]

    reverse = sort_dir == "desc"
    if sort_by == "price":
        products.sort(key=lambda p: float(p["variants"][0]["price"]) if p.get("variants") else 0, reverse=reverse)
    elif sort_by == "stock":
        products.sort(key=lambda p: sum(v.get("inventory_quantity",0) for v in p.get("variants",[])), reverse=reverse)
    elif sort_by == "updated":
        products.sort(key=lambda p: p.get("updated_at",""), reverse=reverse)
    else:
        products.sort(key=lambda p: p.get("title","").lower(), reverse=reverse)

    for p in products:
        p["_price_health"] = _calc_price_health(p)
        p["_completeness"] = _calc_completeness(p)

    total = len(products)
    start = (page-1)*per_page
    return {"products": products[start:start+per_page], "total": total, "page": page, "per_page": per_page, "pages": max(1,-(-total//per_page))}

@app.get("/api/products/{product_id}")
async def get_product(product_id: int, session=Depends(verify_session)):
    data = await shopify._get(f"/products/{product_id}.json")
    p = data.get("product", {})
    p["_price_health"] = _calc_price_health(p)
    p["_completeness"] = _calc_completeness(p)
    return p

@app.put("/api/products/{product_id}")
async def update_product(product_id: int, request: Request, session=Depends(verify_session)):
    data = await request.json()
    return await shopify.update_product(product_id, data)

@app.put("/api/products/{product_id}/variants/{variant_id}")
async def update_variant(product_id: int, variant_id: int, request: Request, session=Depends(verify_session)):
    data = await request.json()
    return await shopify.update_variant(product_id, variant_id, data)

@app.get("/api/collections")
async def get_collections(session=Depends(verify_session)):
    return await shopify.get_collections()

@app.get("/api/tags")
async def get_tags(session=Depends(verify_session)):
    return await shopify.get_all_tags()

@app.get("/api/locations")
async def get_locations(session=Depends(verify_session)):
    return {"locations": await shopify.get_locations()}

@app.get("/api/vendors")
async def get_vendors(session=Depends(verify_session)):
    products = await shopify.get_all_products()
    vendors = sorted(set(p.get("vendor","") for p in products if p.get("vendor","")))
    return {"vendors": vendors}

# ── Store Stats ───────────────────────────────────────────────────────────────
@app.get("/api/store/stats")
async def store_stats(session=Depends(verify_session)):
    products = await shopify.get_all_products()
    orders = await shopify.get_all_orders(days=30)
    return _calc_store_stats(products, orders)

@app.get("/api/briefing")
async def morning_briefing(session=Depends(verify_session)):
    products = await shopify.get_all_products()
    orders_today = await shopify.get_all_orders(days=1)
    orders_30 = await shopify.get_all_orders(days=30)
    briefing = generate_briefing(products, orders_today, orders_30, COST_CONFIG)
    # Add Postex COD pending if token available
    if os.getenv("POSTEX_TOKEN"):
        try:
            px = await postex.get_dashboard_summary(days=30)
            briefing["postex"] = {
                "pending_cod": px.get("pending_cod", 0),
                "in_transit": px.get("in_transit", 0),
                "delivery_rate": px.get("delivery_rate", 0),
            }
        except:
            briefing["postex"] = None
    return briefing

# ── Orders ────────────────────────────────────────────────────────────────────
@app.get("/api/orders")
async def get_orders(
    days: int = 30, status: str = "any",
    from_date: str = None, to_date: str = None,
    city: str = None, min_amount: float = None,
    session=Depends(verify_session)
):
    if from_date and to_date:
        # Custom date range
        orders = await shopify.get_all_orders_range(from_date, to_date, status)
    else:
        orders = await shopify.get_all_orders(status=status, days=days)

    if city:
        orders = [o for o in orders if city.lower() in str(o.get("shipping_address",{}).get("city","")).lower()]
    if min_amount:
        orders = [o for o in orders if float(o.get("total_price","0")) >= min_amount]

    return {"orders": orders, "total": len(orders)}

@app.get("/api/orders/analytics")
async def orders_analytics(days: int = 30, from_date: str = None, to_date: str = None, session=Depends(verify_session)):
    if from_date and to_date:
        orders = await shopify.get_all_orders_range(from_date, to_date)
    else:
        orders = await shopify.get_all_orders(days=days)
    return _calc_order_analytics(orders)

@app.get("/api/orders/{order_id}")
async def get_order(order_id: int, session=Depends(verify_session)):
    data = await shopify._get(f"/orders/{order_id}.json")
    return data.get("order", {})

@app.put("/api/orders/{order_id}/fulfill")
async def fulfill_order(order_id: int, request: Request, session=Depends(verify_session)):
    data = await request.json()
    tracking = data.get("tracking_number", "")
    # Get fulfillment order
    try:
        fo_data = await shopify._get(f"/orders/{order_id}/fulfillment_orders.json")
        fo_id = fo_data["fulfillment_orders"][0]["id"]
        payload = {
            "fulfillment": {
                "line_items_by_fulfillment_order": [{"fulfillment_order_id": fo_id}],
                "tracking_info": {"number": tracking} if tracking else {}
            }
        }
        result = await shopify._post("/fulfillments.json", payload)
        return {"ok": True, "fulfillment": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/abandoned")
async def abandoned_carts(session=Depends(verify_session)):
    checkouts = await shopify.get_abandoned_checkouts()
    total_value = sum(float(c.get("total_price","0")) for c in checkouts)
    return {"checkouts": checkouts[:50], "total": len(checkouts), "total_value": round(total_value, 2)}

# ── Customers ─────────────────────────────────────────────────────────────────
@app.get("/api/customers")
async def get_customers(page: int = 1, per_page: int = 50, search: str = None, session=Depends(verify_session)):
    customers = await shopify.get_all_customers()
    if search:
        s = search.lower()
        customers = [c for c in customers if s in f"{c.get('first_name','')} {c.get('last_name','')} {c.get('email','')}".lower()]
    customers.sort(key=lambda c: float(c.get("total_spent","0")), reverse=True)
    total = len(customers)
    start = (page-1)*per_page
    return {"customers": customers[start:start+per_page], "total": total, "pages": max(1,-(-total//per_page))}

# ── Postex ────────────────────────────────────────────────────────────────────
@app.get("/api/postex/summary")
async def postex_summary(days: int = 30, session=Depends(verify_session)):
    return await postex.get_dashboard_summary(days=days)

@app.get("/api/postex/orders")
async def postex_orders(
    from_date: str = None, to_date: str = None,
    status_id: str = None, page: int = 1,
    session=Depends(verify_session)
):
    if not from_date:
        from_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    if not to_date:
        to_date = datetime.now().strftime("%Y-%m-%d")
    return await postex.get_orders(status_id=status_id, from_date=from_date, to_date=to_date, page=page)

@app.get("/api/postex/track/{tracking_number}")
async def track_postex(tracking_number: str, session=Depends(verify_session)):
    return await postex.track_order(tracking_number)

@app.get("/api/postex/statuses")
async def postex_statuses(session=Depends(verify_session)):
    return await postex.get_order_statuses()

# ── Operations ────────────────────────────────────────────────────────────────
@app.post("/api/operations/preview")
async def preview_operation(request: Request, session=Depends(verify_session)):
    data = await request.json()
    products = await _resolve_scope(data.get("scope", {}))
    return {
        "product_count": len(products),
        "sample": [{"id": p["id"], "title": p["title"]} for p in products[:5]],
        "warning": "This will affect ALL products" if data.get("scope", {}).get("type") == "all" else None
    }

@app.post("/api/operations/run")
async def execute_operation(request: Request, session=Depends(verify_session)):
    data = await request.json()
    operation = data.get("operation")
    params = data.get("params", {})
    scope = data.get("scope", {})
    if not operation:
        raise HTTPException(status_code=400, detail="No operation specified")
    products = await _resolve_scope(scope)
    if not products:
        return JSONResponse({"ok": False, "message": "No products matched"})
    log_id = await db.create_operation_log(operation, scope, params, len(products))
    await db.save_snapshot(log_id, products)
    result = await run_operation(operation, products, params, shopify)
    await db.complete_operation_log(log_id, result)
    return {"ok": True, "log_id": str(log_id), "result": result}

# ── Automation ────────────────────────────────────────────────────────────────
@app.get("/api/automation/rules")
async def get_rules(session=Depends(verify_session)):
    return await db.get_automation_rules()

@app.post("/api/automation/rules")
async def create_rule(request: Request, session=Depends(verify_session)):
    data = await request.json()
    rule_id = await db.create_automation_rule(data)
    return {"ok": True, "id": rule_id}

@app.delete("/api/automation/rules/{rule_id}")
async def delete_rule(rule_id: str, session=Depends(verify_session)):
    await db.delete_automation_rule(rule_id)
    return {"ok": True}

@app.post("/api/automation/rules/{rule_id}/run")
async def run_rule(rule_id: str, session=Depends(verify_session)):
    rules = await db.get_automation_rules()
    rule = next((r for r in rules.get("rules",[]) if r["id"] == rule_id), None)
    if not rule:
        raise HTTPException(status_code=404)
    products = await shopify.get_all_products()
    matched = _apply_rule_filter(products, rule)
    if not matched:
        return {"ok": True, "message": "No products matched rule conditions", "count": 0}
    result = await run_operation(rule["action"], matched, rule.get("action_params",{}), shopify)
    return {"ok": True, "count": len(matched), "result": result}

# ── History ───────────────────────────────────────────────────────────────────
@app.get("/api/history")
async def get_history(page: int = 1, session=Depends(verify_session)):
    return await db.get_operation_logs(page=page)

@app.post("/api/history/{log_id}/rollback")
async def rollback(log_id: str, session=Depends(verify_session)):
    snapshot = await db.get_snapshot(log_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found or expired (30 day limit)")
    products = snapshot["snapshot_data"]
    restored = errors = 0
    for product in products:
        try:
            await shopify.restore_product(product)
            restored += 1
        except:
            errors += 1
    await db.create_operation_log(f"ROLLBACK of {log_id}", {"type":"rollback"}, {}, restored)
    return {"ok": True, "restored": restored, "errors": errors}

# ── Notifications ─────────────────────────────────────────────────────────────
@app.get("/api/notifications")
async def get_notifications(session=Depends(verify_session)):
    return await db.get_notifications()

@app.post("/api/notifications/read-all")
async def mark_all_read(session=Depends(verify_session)):
    await db.mark_notifications_read()
    return {"ok": True}

# ── Saved views ───────────────────────────────────────────────────────────────
@app.get("/api/saved-views")
async def get_saved_views(session=Depends(verify_session)):
    return await db.get_saved_views()

@app.post("/api/saved-views")
async def create_saved_view(request: Request, session=Depends(verify_session)):
    data = await request.json()
    view_id = await db.create_saved_view(data)
    return {"ok": True, "id": view_id}

@app.delete("/api/saved-views/{view_id}")
async def delete_saved_view(view_id: str, session=Depends(verify_session)):
    await db.delete_saved_view(view_id)
    return {"ok": True}

# ── Cost settings ─────────────────────────────────────────────────────────────
@app.get("/api/settings/costs")
async def get_cost_settings(session=Depends(verify_session)):
    return await db.get_cost_settings()

@app.post("/api/settings/costs")
async def update_cost_settings(request: Request, session=Depends(verify_session)):
    data = await request.json()
    await db.update_cost_settings(data)
    return {"ok": True}

# ── Accounting: Vendors ──────────────────────────────────────────────────────
@app.get("/api/accounting/vendors")
async def list_vendors(session=Depends(verify_session)):
    return {"vendors": await db.list_vendors()}

@app.post("/api/accounting/vendors")
async def create_vendor(request: Request, session=Depends(verify_session)):
    data = await request.json()
    row = await db.create_vendor({
        "name": data.get("name", "Unnamed vendor"),
        "type": data.get("type", "other"),
        "contact": data.get("contact", ""),
        "notes": data.get("notes", ""),
    })
    return {"ok": True, "vendor": row}

@app.put("/api/accounting/vendors/{vendor_id}")
async def update_vendor(vendor_id: str, request: Request, session=Depends(verify_session)):
    await db.update_vendor(vendor_id, await request.json())
    return {"ok": True}

@app.delete("/api/accounting/vendors/{vendor_id}")
async def delete_vendor(vendor_id: str, session=Depends(verify_session)):
    await db.delete_vendor(vendor_id)
    return {"ok": True}

# ── Accounting: Vendor purchases ─────────────────────────────────────────────
@app.get("/api/accounting/purchases")
async def list_purchases(from_date: str = None, to_date: str = None, session=Depends(verify_session)):
    return {"purchases": await db.list_vendor_purchases(from_date, to_date)}

@app.post("/api/accounting/purchases")
async def create_purchase(request: Request, session=Depends(verify_session)):
    data = await request.json()
    qty = float(data.get("quantity") or 0)
    unit_cost = float(data.get("unit_cost") or 0)
    total_cost = float(data.get("total_cost") or (qty * unit_cost) or 0)
    paid = float(data.get("amount_paid") or (total_cost if data.get("payment_status", "paid") == "paid" else 0))
    row = await db.create_vendor_purchase({
        "vendor_id": data.get("vendor_id"),
        "category": data.get("category", "other"),
        "description": data.get("description", ""),
        "quantity": qty or None,
        "unit": data.get("unit", ""),
        "unit_cost": unit_cost or None,
        "total_cost": total_cost,
        "purchase_date": data.get("purchase_date") or datetime.utcnow().date().isoformat(),
        "payment_status": data.get("payment_status", "paid"),
        "amount_paid": paid,
        "amount_due": round(total_cost - paid, 2),
        "receipt_note": data.get("receipt_note", ""),
    })
    return {"ok": True, "purchase": row}

@app.delete("/api/accounting/purchases/{purchase_id}")
async def delete_purchase(purchase_id: str, session=Depends(verify_session)):
    await db.delete_vendor_purchase(purchase_id)
    return {"ok": True}

# ── Accounting: Printers ─────────────────────────────────────────────────────
@app.get("/api/accounting/printers")
async def list_printers(session=Depends(verify_session)):
    printers = await db.list_printers()
    for p in printers:
        p["_depreciation"] = straight_line_depreciation(p)
    return {"printers": printers}

@app.post("/api/accounting/printers")
async def create_printer(request: Request, session=Depends(verify_session)):
    data = await request.json()
    row = await db.create_printer({
        "name": data.get("name", "Unnamed printer"),
        "model": data.get("model", ""),
        "vendor_id": data.get("vendor_id"),
        "purchase_cost": float(data.get("purchase_cost") or 0),
        "purchase_date": data.get("purchase_date"),
        "useful_life_years": float(data.get("useful_life_years") or 3),
        "salvage_value": float(data.get("salvage_value") or 0),
        "status": data.get("status", "active"),
    })
    return {"ok": True, "printer": row}

@app.put("/api/accounting/printers/{printer_id}")
async def update_printer(printer_id: str, request: Request, session=Depends(verify_session)):
    await db.update_printer(printer_id, await request.json())
    return {"ok": True}

@app.delete("/api/accounting/printers/{printer_id}")
async def delete_printer(printer_id: str, session=Depends(verify_session)):
    await db.delete_printer(printer_id)
    return {"ok": True}

@app.post("/api/accounting/printers/{printer_id}/usage")
async def add_printer_usage(printer_id: str, request: Request, session=Depends(verify_session)):
    data = await request.json()
    total = await db.log_printer_usage(
        printer_id, float(data.get("hours") or 0),
        data.get("log_date"), data.get("notes", "")
    )
    return {"ok": True, "total_hours": total}

@app.get("/api/accounting/printers/{printer_id}/usage")
async def get_printer_usage(printer_id: str, session=Depends(verify_session)):
    return {"logs": await db.list_printer_usage(printer_id)}

@app.get("/api/accounting/maintenance")
async def list_maintenance(printer_id: str = None, from_date: str = None, to_date: str = None, session=Depends(verify_session)):
    return {"logs": await db.list_printer_maintenance(printer_id, from_date, to_date)}

@app.post("/api/accounting/maintenance")
async def add_maintenance(request: Request, session=Depends(verify_session)):
    data = await request.json()
    row = await db.log_printer_maintenance({
        "printer_id": data.get("printer_id"),
        "log_date": data.get("log_date") or datetime.utcnow().date().isoformat(),
        "type": data.get("type", "routine"),
        "description": data.get("description", ""),
        "cost": float(data.get("cost") or 0),
    })
    return {"ok": True, "log": row}

@app.delete("/api/accounting/maintenance/{log_id}")
async def delete_maintenance(log_id: str, session=Depends(verify_session)):
    await db.delete_printer_maintenance(log_id)
    return {"ok": True}

# ── Accounting: Custom orders ────────────────────────────────────────────────
@app.get("/api/accounting/custom-orders")
async def list_custom_orders(from_date: str = None, to_date: str = None, session=Depends(verify_session)):
    return {"orders": await db.list_custom_orders(from_date, to_date)}

@app.post("/api/accounting/custom-orders")
async def create_custom_order(request: Request, session=Depends(verify_session)):
    data = await request.json()
    row = await db.create_custom_order({
        "customer_name": data.get("customer_name", "Unnamed customer"),
        "contact": data.get("contact", ""),
        "description": data.get("description", ""),
        "sale_price": float(data.get("sale_price") or 0),
        "tax_amount": float(data.get("tax_amount") or 0),
        "cost_estimate": float(data.get("cost_estimate") or 0),
        "payment_status": data.get("payment_status", "unpaid"),
        "amount_paid": float(data.get("amount_paid") or 0),
        "production_type": data.get("production_type", "in_house"),
        "status": data.get("status", "quoted"),
        "order_date": data.get("order_date") or datetime.utcnow().date().isoformat(),
        "delivery_date": data.get("delivery_date"),
        "notes": data.get("notes", ""),
    })
    return {"ok": True, "order": row}

@app.put("/api/accounting/custom-orders/{order_id}")
async def update_custom_order(order_id: str, request: Request, session=Depends(verify_session)):
    await db.update_custom_order(order_id, await request.json())
    return {"ok": True}

@app.delete("/api/accounting/custom-orders/{order_id}")
async def delete_custom_order(order_id: str, session=Depends(verify_session)):
    await db.delete_custom_order(order_id)
    return {"ok": True}

# ── Accounting: Outsourced production ────────────────────────────────────────
@app.get("/api/accounting/outsourced")
async def list_outsourced(from_date: str = None, to_date: str = None, session=Depends(verify_session)):
    return {"jobs": await db.list_outsourced_jobs(from_date, to_date)}

@app.post("/api/accounting/outsourced")
async def create_outsourced(request: Request, session=Depends(verify_session)):
    data = await request.json()
    row = await db.create_outsourced_job({
        "vendor_id": data.get("vendor_id"),
        "order_ref": data.get("order_ref", ""),
        "description": data.get("description", ""),
        "quantity": int(data.get("quantity") or 1),
        "cost_charged": float(data.get("cost_charged") or 0),
        "date_sent": data.get("date_sent") or datetime.utcnow().date().isoformat(),
        "expected_return_date": data.get("expected_return_date"),
        "actual_return_date": data.get("actual_return_date"),
        "status": data.get("status", "sent"),
        "notes": data.get("notes", ""),
    })
    return {"ok": True, "job": row}

@app.put("/api/accounting/outsourced/{job_id}")
async def update_outsourced(job_id: str, request: Request, session=Depends(verify_session)):
    await db.update_outsourced_job(job_id, await request.json())
    return {"ok": True}

@app.delete("/api/accounting/outsourced/{job_id}")
async def delete_outsourced(job_id: str, session=Depends(verify_session)):
    await db.delete_outsourced_job(job_id)
    return {"ok": True}

# ── Accounting: Expenses ─────────────────────────────────────────────────────
@app.get("/api/accounting/expenses")
async def list_expenses_route(from_date: str = None, to_date: str = None, session=Depends(verify_session)):
    return {"expenses": await db.list_expenses(from_date, to_date)}

@app.post("/api/accounting/expenses")
async def create_expense(request: Request, session=Depends(verify_session)):
    data = await request.json()
    row = await db.create_expense({
        "category": data.get("category", "other"),
        "description": data.get("description", ""),
        "amount": float(data.get("amount") or 0),
        "expense_date": data.get("expense_date") or datetime.utcnow().date().isoformat(),
        "recurring": bool(data.get("recurring", False)),
        "recurrence_period": data.get("recurrence_period"),
        "vendor_id": data.get("vendor_id"),
    })
    return {"ok": True, "expense": row}

@app.delete("/api/accounting/expenses/{expense_id}")
async def delete_expense(expense_id: str, session=Depends(verify_session)):
    await db.delete_expense(expense_id)
    return {"ok": True}

# ── Accounting: Employees & payments ─────────────────────────────────────────
@app.get("/api/accounting/employees")
async def list_employees_route(session=Depends(verify_session)):
    return {"employees": await db.list_employees()}

@app.post("/api/accounting/employees")
async def create_employee(request: Request, session=Depends(verify_session)):
    data = await request.json()
    row = await db.create_employee({
        "name": data.get("name", "Unnamed"),
        "role": data.get("role", ""),
        "payment_type": data.get("payment_type", "fixed_salary"),
        "rate": float(data.get("rate") or 0),
        "contact": data.get("contact", ""),
        "active": bool(data.get("active", True)),
        "start_date": data.get("start_date"),
    })
    return {"ok": True, "employee": row}

@app.put("/api/accounting/employees/{employee_id}")
async def update_employee(employee_id: str, request: Request, session=Depends(verify_session)):
    await db.update_employee(employee_id, await request.json())
    return {"ok": True}

@app.delete("/api/accounting/employees/{employee_id}")
async def delete_employee(employee_id: str, session=Depends(verify_session)):
    await db.delete_employee(employee_id)
    return {"ok": True}

@app.get("/api/accounting/employees/payments")
async def list_employee_payments_route(employee_id: str = None, from_date: str = None, to_date: str = None, session=Depends(verify_session)):
    return {"payments": await db.list_employee_payments(employee_id, from_date, to_date)}

@app.post("/api/accounting/employees/payments")
async def create_employee_payment(request: Request, session=Depends(verify_session)):
    data = await request.json()
    row = await db.create_employee_payment({
        "employee_id": data.get("employee_id"),
        "amount": float(data.get("amount") or 0),
        "payment_date": data.get("payment_date") or datetime.utcnow().date().isoformat(),
        "period_covered": data.get("period_covered", ""),
        "notes": data.get("notes", ""),
    })
    return {"ok": True, "payment": row}

@app.delete("/api/accounting/employees/payments/{payment_id}")
async def delete_employee_payment(payment_id: str, session=Depends(verify_session)):
    await db.delete_employee_payment(payment_id)
    return {"ok": True}

# ── Accounting: Cash accounts ────────────────────────────────────────────────
@app.get("/api/accounting/cash-accounts")
async def list_cash_accounts_route(session=Depends(verify_session)):
    accounts = await db.list_cash_accounts()
    for a in accounts:
        txns = await db.list_cash_transactions(a["id"])
        a["_balance"] = calc_cash_balance(a, txns)
    return {"accounts": accounts}

@app.post("/api/accounting/cash-accounts")
async def create_cash_account(request: Request, session=Depends(verify_session)):
    data = await request.json()
    row = await db.create_cash_account({
        "name": data.get("name", "Unnamed account"),
        "account_type": data.get("account_type", "bank"),
        "opening_balance": float(data.get("opening_balance") or 0),
    })
    return {"ok": True, "account": row}

@app.delete("/api/accounting/cash-accounts/{account_id}")
async def delete_cash_account(account_id: str, session=Depends(verify_session)):
    await db.delete_cash_account(account_id)
    return {"ok": True}

@app.get("/api/accounting/cash-transactions")
async def list_cash_transactions_route(account_id: str = None, from_date: str = None, to_date: str = None, session=Depends(verify_session)):
    return {"transactions": await db.list_cash_transactions(account_id, from_date, to_date)}

@app.post("/api/accounting/cash-transactions")
async def create_cash_transaction(request: Request, session=Depends(verify_session)):
    data = await request.json()
    row = await db.create_cash_transaction({
        "account_id": data.get("account_id"),
        "direction": data.get("direction", "in"),
        "amount": float(data.get("amount") or 0),
        "category": data.get("category", "other"),
        "reference": data.get("reference", ""),
        "txn_date": data.get("txn_date") or datetime.utcnow().date().isoformat(),
        "notes": data.get("notes", ""),
    })
    return {"ok": True, "transaction": row}

@app.delete("/api/accounting/cash-transactions/{txn_id}")
async def delete_cash_transaction(txn_id: str, session=Depends(verify_session)):
    await db.delete_cash_transaction(txn_id)
    return {"ok": True}

# ── Accounting: PostEx payouts & reconciliation ──────────────────────────────
@app.get("/api/accounting/postex-payouts")
async def list_postex_payouts_route(from_date: str = None, to_date: str = None, session=Depends(verify_session)):
    return {"payouts": await db.list_postex_payouts(from_date, to_date)}

@app.post("/api/accounting/postex-payouts")
async def create_postex_payout(request: Request, session=Depends(verify_session)):
    data = await request.json()
    gross = float(data.get("gross_amount") or 0)
    fee = float(data.get("fee_deducted") or 0)
    net = float(data.get("net_amount") or (gross - fee))
    row = await db.create_postex_payout({
        "payout_date": data.get("payout_date") or datetime.utcnow().date().isoformat(),
        "gross_amount": gross,
        "fee_deducted": fee,
        "net_amount": net,
        "order_count": int(data.get("order_count") or 0),
        "notes": data.get("notes", ""),
    })
    return {"ok": True, "payout": row}

@app.delete("/api/accounting/postex-payouts/{payout_id}")
async def delete_postex_payout(payout_id: str, session=Depends(verify_session)):
    await db.delete_postex_payout(payout_id)
    return {"ok": True}

@app.get("/api/accounting/postex-reconcile")
async def postex_reconcile(days: int = 30, session=Depends(verify_session)):
    summary = await postex.get_dashboard_summary(days=days)
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = datetime.now().strftime("%Y-%m-%d")
    payouts = await db.list_postex_payouts(from_date, to_date)
    return reconcile_postex(summary, payouts)

# ── Accounting: P&L ───────────────────────────────────────────────────────────
@app.get("/api/accounting/pnl")
async def accounting_pnl(from_date: str = None, to_date: str = None, session=Depends(verify_session)):
    if not to_date:
        to_date = datetime.utcnow().date().isoformat()
    if not from_date:
        from_date = (datetime.utcnow() - timedelta(days=30)).date().isoformat()

    shopify_orders = await shopify.get_all_orders_range(from_date, to_date)
    custom_orders = await db.list_custom_orders(from_date, to_date)
    vendor_purchases = await db.list_vendor_purchases(from_date, to_date)
    outsourced_jobs = await db.list_outsourced_jobs(from_date, to_date)
    expenses = await db.list_expenses(from_date, to_date)
    employee_payments = await db.list_employee_payments(None, from_date, to_date)
    printer_maintenance = await db.list_printer_maintenance(None, from_date, to_date)
    printers = await db.list_printers()
    postex_payouts = await db.list_postex_payouts(from_date, to_date)

    return generate_pnl(
        from_date, to_date,
        shopify_orders=shopify_orders, custom_orders=custom_orders,
        vendor_purchases=vendor_purchases, outsourced_jobs=outsourced_jobs,
        expenses=expenses, employee_payments=employee_payments,
        printer_maintenance=printer_maintenance, printers=printers,
        postex_payouts=postex_payouts,
    )

@app.get("/api/accounting/export")
async def accounting_export(from_date: str = None, to_date: str = None, session=Depends(verify_session)):
    if not to_date:
        to_date = datetime.utcnow().date().isoformat()
    if not from_date:
        from_date = (datetime.utcnow() - timedelta(days=30)).date().isoformat()

    pnl = await accounting_pnl(from_date, to_date, session)
    purchases = await db.list_vendor_purchases(from_date, to_date)
    expenses = await db.list_expenses(from_date, to_date)
    custom_orders = await db.list_custom_orders(from_date, to_date)
    payments = await db.list_employee_payments(None, from_date, to_date)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"TD Shopify Manager -- P&L export {from_date} to {to_date}"])
    w.writerow([])
    w.writerow(["Revenue (Shopify)", pnl["revenue"]["shopify"]])
    w.writerow(["Revenue (Custom orders)", pnl["revenue"]["custom_orders"]])
    w.writerow(["Total revenue", pnl["revenue"]["total"]])
    w.writerow(["Tax included in revenue", pnl["revenue"]["tax_included"]])
    w.writerow([])
    w.writerow(["Materials & packaging", pnl["cogs"]["materials_packaging"]])
    w.writerow(["Outsourced production", pnl["cogs"]["outsourced_production"]])
    w.writerow(["Printer maintenance", pnl["cogs"]["printer_maintenance"]])
    w.writerow(["Printer depreciation", pnl["cogs"]["printer_depreciation"]])
    w.writerow(["Total COGS", pnl["cogs"]["total"]])
    w.writerow([])
    for cat, amt in pnl["opex"]["by_category"].items():
        w.writerow([f"Expense: {cat}", amt])
    w.writerow(["Total operating expenses", pnl["opex"]["total"]])
    w.writerow(["Courier fees", pnl["courier_fees"]])
    w.writerow(["Labor cost", pnl["labor_cost"]])
    w.writerow([])
    w.writerow(["Total costs", pnl["total_costs"]])
    w.writerow(["Net profit", pnl["net_profit"]])
    w.writerow(["Margin %", pnl["margin_pct"]])
    w.writerow(["Estimated cash collected", pnl["cash_collected_estimate"]])
    w.writerow([])
    w.writerow(["-- Vendor purchases --"])
    w.writerow(["Date", "Category", "Description", "Total cost", "Payment status"])
    for p in purchases:
        w.writerow([p.get("purchase_date"), p.get("category"), p.get("description"), p.get("total_cost"), p.get("payment_status")])
    w.writerow([])
    w.writerow(["-- Expenses --"])
    w.writerow(["Date", "Category", "Description", "Amount"])
    for e in expenses:
        w.writerow([e.get("expense_date"), e.get("category"), e.get("description"), e.get("amount")])
    w.writerow([])
    w.writerow(["-- Custom orders --"])
    w.writerow(["Date", "Customer", "Sale price", "Payment status"])
    for c in custom_orders:
        w.writerow([c.get("order_date"), c.get("customer_name"), c.get("sale_price"), c.get("payment_status")])
    w.writerow([])
    w.writerow(["-- Employee payments --"])
    w.writerow(["Date", "Amount", "Period covered", "Notes"])
    for pay in payments:
        w.writerow([pay.get("payment_date"), pay.get("amount"), pay.get("period_covered"), pay.get("notes")])

    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="tdpk_accounts_{from_date}_to_{to_date}.csv"'}
    )

# ── Helpers ───────────────────────────────────────────────────────────────────
async def _resolve_scope(scope):
    t = scope.get("type","all")
    if t == "ids":
        return await shopify.get_products_by_ids(scope.get("value",[]))
    elif t == "collection":
        return await shopify.get_all_products(collection_id=scope.get("value"))
    elif t == "tag":
        return await shopify.get_all_products(tag=scope.get("value"))
    elif t == "status":
        return await shopify.get_all_products(status=scope.get("value"))
    elif t == "price_range":
        v = scope.get("value",{})
        all_p = await shopify.get_all_products()
        return [p for p in all_p if _price_in_range(p, v.get("min"), v.get("max"))]
    else:
        return await shopify.get_all_products()

def _price_in_range(p, min_p, max_p):
    prices = [float(v["price"]) for v in p.get("variants",[]) if v.get("price")]
    if not prices: return False
    avg = sum(prices)/len(prices)
    if min_p is not None and avg < min_p: return False
    if max_p is not None and avg > max_p: return False
    return True

def _calc_price_health(product):
    prices = [float(v.get("price",0)) for v in product.get("variants",[]) if float(v.get("price",0))>0]
    if not prices: return {"status":"red","label":"No price","margin":0}
    avg = sum(prices)/len(prices)
    total_cost = COST_CONFIG["avg_filament_per_order"] + COST_CONFIG["avg_packaging_shipping"] + COST_CONFIG["ad_spend_per_order"] + (COST_CONFIG["fixed_monthly"]/COST_CONFIG["target_monthly_orders"])
    if avg == 0: return {"status":"red","label":"Zero price","margin":0}
    margin = ((avg - total_cost) / avg) * 100
    if margin < 0: return {"status":"red","label":f"Loss ({margin:.0f}%)","margin":round(margin,1)}
    elif margin < 20: return {"status":"orange","label":f"Thin ({margin:.0f}%)","margin":round(margin,1)}
    elif margin < 35: return {"status":"yellow","label":f"OK ({margin:.0f}%)","margin":round(margin,1)}
    elif margin < 50: return {"status":"green","label":f"Good ({margin:.0f}%)","margin":round(margin,1)}
    else: return {"status":"blue","label":f"Excellent ({margin:.0f}%)","margin":round(margin,1)}

def _calc_completeness(product):
    checks = {
        "image": bool(product.get("images")),
        "description": len((product.get("body_html") or "").strip()) > 50,
        "price": any(float(v.get("price",0))>0 for v in product.get("variants",[])),
        "tags": bool(product.get("tags","").strip()),
        "type": bool(product.get("product_type","").strip()),
        "vendor": bool(product.get("vendor","").strip()),
    }
    score = sum(1 for v in checks.values() if v)
    return {"score": round((score/len(checks))*100), "checks": checks}

def _calc_store_stats(products, orders):
    total = len(products)
    active = sum(1 for p in products if p.get("status")=="active")
    draft = sum(1 for p in products if p.get("status")=="draft")
    zero_price = sum(1 for p in products if all(float(v.get("price",0))==0 for v in p.get("variants",[])))
    no_image = sum(1 for p in products if not p.get("images"))
    out_of_stock = sum(1 for p in products if sum(v.get("inventory_quantity",0) for v in p.get("variants",[]))<=0)
    inventory_value = sum(float(v.get("price",0))*max(0,v.get("inventory_quantity",0)) for p in products for v in p.get("variants",[]))
    revenue_30 = sum(float(o.get("total_price","0")) for o in orders)
    order_count = len(orders)
    avg_order = revenue_30/order_count if order_count else 0
    return {
        "products": {"total":total,"active":active,"draft":draft,"zero_price":zero_price,"no_image":no_image,"out_of_stock":out_of_stock},
        "inventory_value": round(inventory_value,0),
        "orders_30d": {"count":order_count,"revenue":round(revenue_30,0),"avg_order":round(avg_order,0)},
    }

def _calc_order_analytics(orders):
    if not orders: return {"total":0,"revenue":0,"avg_order":0,"by_day":{},"by_city":{},"top_products":{}}
    revenue = sum(float(o.get("total_price","0")) for o in orders)
    by_day, by_city, top_products = {}, {}, {}
    for o in orders:
        day = o.get("created_at","")[:10]
        by_day[day] = round(by_day.get(day,0) + float(o.get("total_price","0")), 0)
        city = o.get("shipping_address",{}).get("city","Unknown") if o.get("shipping_address") else "Unknown"
        by_city[city] = by_city.get(city,0) + 1
        for item in o.get("line_items",[]):
            title = item.get("title","Unknown")
            top_products[title] = top_products.get(title,0) + item.get("quantity",0)
    return {
        "total": len(orders),
        "revenue": round(revenue,0),
        "avg_order": round(revenue/len(orders),0),
        "by_day": dict(sorted(by_day.items())),
        "by_city": dict(sorted(by_city.items(), key=lambda x:x[1], reverse=True)[:10]),
        "top_products": dict(sorted(top_products.items(), key=lambda x:x[1], reverse=True)[:10]),
    }

def _apply_rule_filter(products, rule):
    condition = rule.get("condition","")
    value = rule.get("condition_value","")
    matched = []
    for p in products:
        if condition == "tag_contains" and value.lower() in p.get("tags","").lower(): matched.append(p)
        elif condition == "title_contains" and value.lower() in p.get("title","").lower(): matched.append(p)
        elif condition == "status_is" and p.get("status","") == value: matched.append(p)
        elif condition == "price_below":
            prices = [float(v.get("price",0)) for v in p.get("variants",[]) if float(v.get("price",0))>0]
            if prices and min(prices) < float(value or 0): matched.append(p)
        elif condition == "zero_stock":
            if sum(v.get("inventory_quantity",0) for v in p.get("variants",[])) <= 0: matched.append(p)
        elif condition == "no_image" and not p.get("images"): matched.append(p)
    return matched
