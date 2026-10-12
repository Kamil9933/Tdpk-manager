"""
MCP (Model Context Protocol) connector for TD Shopify Manager.

Exposes store/PostEx/accounting data and actions as MCP tools so Claude (or any
other MCP client) can query and update TDPK directly, instead of you copy-pasting
numbers into a chat. Built with the official `mcp` Python SDK's FastMCP server,
mounted into the existing FastAPI app at POST/GET /mcp using the Streamable HTTP
transport (the current standard for remote MCP servers -- works over plain HTTPS,
no separate SSE connection needed).

Auth: deliberately NOT using FastMCP's built-in OAuth machinery -- this is a
single-user private tool, not a multi-tenant service, so a shared bearer token
(MCP_API_KEY env var) checked by a small ASGI middleware in front of the MCP app
is simpler and sufficient. Every request to /mcp must carry
`Authorization: Bearer <MCP_API_KEY>` or it's rejected before it ever reaches the
MCP session logic.

Design choices worth knowing about:
- Read tools return the data as-is (including PostEx's raw, undocumented field
  names) rather than a curated subset, specifically so field-level questions
  ("does this order have a COD/prepaid flag? what's it called?") can be answered
  by just looking at what comes back, instead of guessing wrong in code.
- Write tools cover create/update, not delete. Deleting financial/accounting
  records via a natural-language interface is the kind of action that's easy to
  get wrong once and expensive to get wrong -- do deletes from the dashboard UI
  directly, where you can see what you're deleting.
- Full per-order status HISTORY (booked -> in transit -> delivered, with
  timestamps) is only available per tracking number via track_postex_order, not
  in bulk -- PostEx's bulk list endpoint only reports the current status, not the
  timeline. If you need timelines for many orders at once, that's a PostEx API
  limitation, not something this connector can work around without hitting
  track-order once per tracking number (slow, and not done automatically here to
  avoid hammering PostEx's API on every summary query).
- Product-level return-rate ("does the Rs 5,500 SKU get refused more than the
  Rs 700 one") isn't available yet -- PostEx's order object has no product
  info, only value. get_postex_return_value_breakdown buckets by COD value as a
  rough proxy; a real product-level answer needs a feature that cross-references
  Shopify order numbers against PostEx tracking numbers, which doesn't exist yet.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from core.accounting import generate_pnl, straight_line_depreciation, calc_cash_balance, reconcile_postex
from core.meta import MetaError
from core.operations import run_operation
from core.filament import deduct_filament_for_order, compute_filament_status
from core.returns import compute_product_return_breakdown
from core.notify import notify_all

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time -- fixed UTC+5 offset, no DST


class MCPAuthMiddleware:
    """Wraps the MCP ASGI app so every request must present the shared bearer
    token. Fails closed: if MCP_API_KEY isn't set at all, every request is
    rejected rather than the connector silently running unauthenticated."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # This is mounted at the FastAPI root ("/") so it never shadows any of
        # the app's other specific routes (see main.py), but that also means it
        # sees every path nothing else matched -- not just /mcp. Only enforce
        # the token check for the actual MCP path; let anything else fall
        # through to the inner app so a random typo'd URL still 404s normally
        # instead of confusingly reporting "Unauthorized".
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if scope["type"] != "http" or path.rstrip("/") != "/mcp":
            await self.app(scope, receive, send)
            return

        expected_key = os.getenv("MCP_API_KEY", "")
        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("utf-8", "ignore")

        if not expected_key or auth_header != f"Bearer {expected_key}":
            response = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _default_range(from_date, to_date, days=30):
    if not to_date:
        to_date = datetime.utcnow().date().isoformat()
    if not from_date:
        from_date = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
    return from_date, to_date


def build_mcp_server(shopify, postex, db, meta, resolve_scope) -> FastMCP:
    """Construct the FastMCP server and register every tool. Takes the already-
    constructed shopify/postex clients and the db singleton so tools share the
    exact same clients (and therefore the exact same auth/token handling) the
    rest of the app uses -- no separate credentials to configure. `resolve_scope`
    is main.py's own scope-resolution function (ids/collection/tag/status/
    price_range -> product list), reused as-is by the bulk catalog-operation
    tool so there's exactly one implementation of "which products does this
    apply to", shared with the dashboard's own Operations panel."""

    # FastMCP's DNS-rebinding protection only allows localhost/127.0.0.1 Host
    # headers by default -- which would reject every real request once this is
    # deployed publicly on Render. Allow Render's own hostname (it sets
    # RENDER_EXTERNAL_HOSTNAME automatically) plus anything listed in
    # MCP_ALLOWED_HOSTS (comma-separated -- set this if you put a custom domain
    # in front), on top of localhost for local testing.
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    render_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "")
    if render_host:
        allowed_hosts.append(render_host)
    for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(","):
        h = h.strip()
        if h:
            allowed_hosts.append(h)

    mcp = FastMCP(
        "TD Shopify Manager",
        instructions=(
            "Tools for thirddimension.pk's private admin backend: Shopify store "
            "data, PostEx COD courier data, and a manual accounting ledger "
            "(vendors, printers, custom orders, outsourced production, expenses, "
            "employees, cash accounts). Money amounts are in PKR unless noted."
        ),
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            # Origin is only sent by browser-driven requests; a server-side MCP
            # client (Claude's remote connector, mcp-remote, curl) sends none at
            # all, and an absent Origin always passes -- so this is only a
            # backstop for someone building a browser-based client later.
            allowed_origins=[f"https://{render_host}"] if render_host else [],
        ),
    )

    # ── Store overview (Shopify) ────────────────────────────────────────────
    @mcp.tool()
    async def get_store_overview(days: int = 30) -> dict:
        """Snapshot of the Shopify store: product counts (active/draft/zero-price/
        no-image/out-of-stock), total inventory value, and order volume/revenue
        for the trailing N days."""
        products = await shopify.get_all_products()
        orders = await shopify.get_all_orders(days=days)
        total = len(products)
        active = sum(1 for p in products if p.get("status") == "active")
        draft = sum(1 for p in products if p.get("status") == "draft")
        zero_price = sum(1 for p in products if all(float(v.get("price", 0)) == 0 for v in p.get("variants", [])))
        no_image = sum(1 for p in products if not p.get("images"))
        out_of_stock = sum(1 for p in products if sum(v.get("inventory_quantity", 0) for v in p.get("variants", [])) <= 0)
        inventory_value = sum(float(v.get("price", 0)) * max(0, v.get("inventory_quantity", 0)) for p in products for v in p.get("variants", []))
        revenue = sum(float(o.get("total_price", "0")) for o in orders)
        return {
            "products": {"total": total, "active": active, "draft": draft, "zero_price": zero_price, "no_image": no_image, "out_of_stock": out_of_stock},
            "inventory_value": round(inventory_value, 0),
            "orders": {"days": days, "count": len(orders), "revenue": round(revenue, 0), "avg_order": round(revenue / len(orders), 0) if orders else 0},
        }

    @mcp.tool()
    async def list_orders(
        days: int = 30, from_date: Optional[str] = None, to_date: Optional[str] = None,
        status: str = "any", city: Optional[str] = None, min_amount: Optional[float] = None, limit: int = 50,
    ) -> dict:
        """List Shopify orders. Use from_date/to_date (YYYY-MM-DD) for an exact
        range, or `days` for a trailing window. Filter by shipping city
        (substring match) or a minimum order total. Returns trimmed order info
        (id, name, date, total, financial/fulfillment status, city, customer)."""
        if from_date and to_date:
            orders = await shopify.get_all_orders_range(from_date, to_date, status)
        else:
            orders = await shopify.get_all_orders(status=status, days=days)
        if city:
            orders = [o for o in orders if city.lower() in str(o.get("shipping_address", {}).get("city", "")).lower()]
        if min_amount:
            orders = [o for o in orders if float(o.get("total_price", "0")) >= min_amount]
        trimmed = [{
            "id": o.get("id"), "name": o.get("name"), "created_at": o.get("created_at"),
            "total_price": o.get("total_price"), "financial_status": o.get("financial_status"),
            "fulfillment_status": o.get("fulfillment_status"),
            "city": (o.get("shipping_address") or {}).get("city"),
            "customer": f"{(o.get('customer') or {}).get('first_name','')} {(o.get('customer') or {}).get('last_name','')}".strip(),
            "phone": o.get("phone") or (o.get("customer") or {}).get("phone"),
        } for o in orders[:limit]]
        return {"orders": trimmed, "total_matched": len(orders), "returned": len(trimmed)}

    @mcp.tool()
    async def get_order(order_id: int) -> dict:
        """Full raw Shopify order detail (line items, addresses, payment/fulfillment status) for one order id."""
        data = await shopify._get(f"/orders/{order_id}.json")
        return data.get("order", {})

    @mcp.tool()
    async def get_product(product_id: int) -> dict:
        """Full raw Shopify product detail (all variants with price/stock/id) for one product id. Use this
        (after search_products to find the id) when you need the exact variant list -- search_products only
        returns a trimmed single-variant price for a quick listing-health check."""
        data = await shopify._get(f"/products/{product_id}.json")
        return data.get("product", {})

    @mcp.tool()
    async def search_products(
        search: Optional[str] = None, vendor: Optional[str] = None, product_type: Optional[str] = None,
        zero_stock: bool = False, zero_price: bool = False, no_image: bool = False, limit: int = 50,
    ) -> dict:
        """Search/filter Shopify products by title/vendor match, vendor, product
        type, or data-quality flags (zero_stock, zero_price, no_image -- handy
        for a quick low-stock or listing-health check)."""
        products = await shopify.get_all_products()
        if search:
            s = search.lower()
            products = [p for p in products if s in p.get("title", "").lower() or s in p.get("vendor", "").lower()]
        if vendor:
            products = [p for p in products if p.get("vendor", "").lower() == vendor.lower()]
        if product_type:
            products = [p for p in products if p.get("product_type", "").lower() == product_type.lower()]
        if zero_stock:
            products = [p for p in products if sum(v.get("inventory_quantity", 0) for v in p.get("variants", [])) <= 0]
        if zero_price:
            products = [p for p in products if all(float(v.get("price", 0)) == 0 for v in p.get("variants", []))]
        if no_image:
            products = [p for p in products if not p.get("images")]
        trimmed = [{
            "id": p.get("id"), "title": p.get("title"), "vendor": p.get("vendor"), "status": p.get("status"),
            "stock": sum(v.get("inventory_quantity", 0) for v in p.get("variants", [])),
            "price": p["variants"][0]["price"] if p.get("variants") else None,
        } for p in products[:limit]]
        return {"products": trimmed, "total_matched": len(products), "returned": len(trimmed)}

    @mcp.tool()
    async def list_low_stock_products(threshold: int = 5) -> dict:
        """Products (with per-variant stock) at or below a stock threshold -- a
        quick low-filament/low-inventory check until a dedicated alert feature exists."""
        products = await shopify.get_all_products()
        low = []
        for p in products:
            for v in p.get("variants", []):
                qty = v.get("inventory_quantity", 0)
                if qty <= threshold:
                    low.append({"product_id": p.get("id"), "title": p.get("title"), "variant": v.get("title"), "stock": qty})
        low.sort(key=lambda x: x["stock"])
        return {"low_stock": low, "threshold": threshold, "count": len(low)}

    @mcp.tool()
    async def list_customers(search: Optional[str] = None, limit: int = 50) -> dict:
        """Shopify customers sorted by total spend, optionally name/email filtered."""
        customers = await shopify.get_all_customers()
        if search:
            s = search.lower()
            customers = [c for c in customers if s in f"{c.get('first_name','')} {c.get('last_name','')} {c.get('email','')}".lower()]
        customers.sort(key=lambda c: float(c.get("total_spent", "0")), reverse=True)
        trimmed = [{
            "id": c.get("id"), "name": f"{c.get('first_name','')} {c.get('last_name','')}".strip(),
            "email": c.get("email"), "phone": c.get("phone"),
            "orders_count": c.get("orders_count"), "total_spent": c.get("total_spent"),
        } for c in customers[:limit]]
        return {"customers": trimmed, "total": len(customers)}

    @mcp.tool()
    async def get_abandoned_checkouts(limit: int = 50) -> dict:
        """Abandoned Shopify checkouts with total value -- candidates for cart-recovery follow-up."""
        checkouts = await shopify.get_abandoned_checkouts()
        total_value = sum(float(c.get("total_price", "0")) for c in checkouts)
        return {"checkouts": checkouts[:limit], "total_count": len(checkouts), "total_value": round(total_value, 2)}

    @mcp.tool()
    async def fulfill_order(order_id: int, tracking_number: str = "") -> dict:
        """Mark a Shopify order as fulfilled, optionally attaching a tracking number. WRITE action."""
        fo_data = await shopify._get(f"/orders/{order_id}/fulfillment_orders.json")
        fo_id = fo_data["fulfillment_orders"][0]["id"]
        payload = {
            "fulfillment": {
                "line_items_by_fulfillment_order": [{"fulfillment_order_id": fo_id}],
                "tracking_info": {"number": tracking_number} if tracking_number else {},
            }
        }
        result = await shopify._post("/fulfillments.json", payload)
        filament_result = await deduct_filament_for_order(db, shopify, order_id)
        return {"ok": True, "fulfillment": result, "filament": filament_result}

    @mcp.tool()
    async def update_variant_price(product_id: int, variant_id: int, price: str, confirm: bool = True) -> dict:
        """Update a single Shopify product variant's price. WRITE action.

        confirm defaults to TRUE -- unlike every other guardrailed tool in this
        file -- deliberately, to preserve existing behaviour: a single,
        explicitly-identified price change has always been treated as a routine
        Tier-3 action Claude/Gemini can do directly, no confirm step. Pass
        confirm=False to get a preview instead without changing anything; this
        is what the Manager's rule-based Tier 1 uses for a name-matched price
        change (e.g. "set price of X to Y"), where showing the exact
        before/after first is safer than trusting a regex match blindly."""
        if not confirm:
            return {
                "preview": True, "action": "Update variant price",
                "details": {"product_id": product_id, "variant_id": variant_id, "new_price": price},
                "note": "Nothing was changed. Call again with confirm=true to proceed.",
            }
        result = await shopify.update_variant(product_id, variant_id, {"price": price})
        return {"ok": True, "variant": result}

    @mcp.tool()
    async def run_bulk_catalog_operation(
        operation: str, scope_type: str = "all", scope_value: Optional[Any] = None,
        params: Optional[dict] = None, confirm: bool = False,
    ) -> dict:
        """Run a bulk catalog operation across a set of products -- the exact
        same engine as the dashboard's Operations panel (colour ops, pricing
        ops, inventory/tax toggles, tags, publish state, vendor/type/weight).

        operation is one of: remove_color, add_color, rename_color,
        price_percentage, price_fixed, price_compare_at, price_remove_sale,
        set_stock, inventory_tracking, continue_selling, disable_tax,
        enable_tax, add_tag, remove_tag, publish, unpublish, set_vendor,
        set_product_type, set_weight.

        scope_type picks which products are affected: "all" (every product in
        the store -- use with real care), "ids" (scope_value = a list of
        product ids), "collection" (scope_value = a collection id), "tag"
        (scope_value = a tag string), "status" (scope_value = "active" or
        "draft"), or "price_range" (scope_value = {"min":.., "max":..}).

        params holds the operation's own arguments -- the same field names the
        dashboard's operation panel uses, e.g. {"color": "Red"} for
        remove_color, {"percentage": 10} for price_percentage (10 = +10%),
        {"tag": "clearance"} for add_tag.

        GUARDRAIL: ALWAYS requires confirm=true, with no automation toggle able
        to skip it, no matter how many products are affected -- a bulk change
        can touch hundreds of products at once. Call once without confirm to
        see exactly how many products match and a 5-item sample, show that to
        the human verbatim, and only call again with confirm=true after their
        explicit approval of that exact preview. Every confirmed run is logged
        and snapshotted, so it can be rolled back from the dashboard's History
        tab within 30 days if something goes wrong."""
        scope = {"type": scope_type, "value": scope_value}
        products = await resolve_scope(scope)
        if not confirm:
            return {
                "preview": True, "action": f"Bulk operation: {operation}",
                "product_count": len(products),
                "sample": [{"id": p["id"], "title": p["title"]} for p in products[:5]],
                "params": params or {},
                "warning": "This will affect ALL products in the store" if scope_type == "all" else None,
                "note": "Nothing was changed. Call again with confirm=true to proceed.",
            }
        if not products:
            return {"ok": False, "message": "No products matched this scope"}
        log_id = await db.create_operation_log(operation, scope, params or {}, len(products))
        await db.save_snapshot(log_id, products)
        result = await run_operation(operation, products, params or {}, shopify)
        await db.complete_operation_log(log_id, result)
        return {"ok": True, "log_id": str(log_id), "product_count": len(products), "result": result}

    @mcp.tool()
    async def cancel_order(order_id: int, reason: str = "customer", restock: bool = True, confirm: bool = False) -> dict:
        """Cancel a Shopify order. reason is one of: customer, inventory,
        fraud, declined, other. restock=true returns the cancelled line items
        to inventory. This does not touch PostEx -- if the order is already
        booked with the courier, cancel it there separately too.

        GUARDRAIL: ALWAYS requires confirm=true, no automation toggle can skip
        this -- cancelling an order is a real, visible change for the
        customer. Preview first, get explicit human approval in chat, then
        confirm."""
        if reason not in ("customer", "inventory", "fraud", "declined", "other"):
            return {"error": "reason must be one of: customer, inventory, fraud, declined, other"}
        if not confirm:
            order = (await shopify._get(f"/orders/{order_id}.json")).get("order", {})
            customer = order.get("customer") or {}
            return {
                "preview": True, "action": "Cancel order",
                "details": {
                    "order_id": order_id, "order_number": order.get("name"), "total": order.get("total_price"),
                    "customer": f"{customer.get('first_name','')} {customer.get('last_name','')}".strip(),
                    "reason": reason, "restock": restock,
                },
                "note": "Nothing was changed. Call again with confirm=true to proceed.",
            }
        result = await shopify._post(f"/orders/{order_id}/cancel.json", {"reason": reason, "restock": restock, "email": False})
        return {"ok": True, "result": result}

    @mcp.tool()
    async def refund_order(order_id: int, line_item_ids: Optional[list] = None, restock: bool = True, note: str = "", confirm: bool = False) -> dict:
        """Process a return/refund record on a Shopify order -- restocks the
        given line items (or all of them if line_item_ids is omitted) and
        creates a refund record on the order.

        IMPORTANT: this store takes cash-on-delivery via PostEx, so Shopify
        never actually captured a payment -- this tool does NOT move any money
        automatically, it only records the return and restocks inventory. If
        the customer is owed cash back, log that separately with
        add_cash_transaction.

        GUARDRAIL: ALWAYS requires confirm=true, no automation toggle can skip
        this. Preview first, get explicit human approval in chat, then
        confirm."""
        order = (await shopify._get(f"/orders/{order_id}.json")).get("order", {})
        items = order.get("line_items", [])
        if line_item_ids:
            items = [li for li in items if li["id"] in line_item_ids]
        refund_line_items = [
            {"line_item_id": li["id"], "quantity": li["quantity"], "restock_type": "return" if restock else "no_restock"}
            for li in items
        ]
        if not confirm:
            return {
                "preview": True, "action": "Refund / return order (no money moved -- this is a COD store)",
                "details": {
                    "order_id": order_id, "order_number": order.get("name"),
                    "items": [{"title": li.get("title"), "quantity": li.get("quantity")} for li in items],
                    "restock": restock, "note": note,
                },
                "note": "Nothing was changed. Call again with confirm=true to proceed.",
            }
        if not items:
            return {"ok": False, "message": "No matching line items found on this order"}
        payload = {"refund": {"notify": False, "note": note, "refund_line_items": refund_line_items, "shipping": {"full_refund": False}}}
        result = await shopify._post(f"/orders/{order_id}/refunds.json", payload)
        return {"ok": True, "result": result}

    # ── PostEx ───────────────────────────────────────────────────────────────
    @mcp.tool()
    async def get_postex_summary(days: int = 30, from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """PostEx COD dashboard summary for a window: delivered/returned/in-transit/
        unbooked counts and rates, COD value totals, top cities, and a full raw
        status_breakdown (every status label PostEx actually sent, unfiltered --
        the reliable way to see whether returns are really being captured).
        Pass from_date/to_date (YYYY-MM-DD) for an arbitrary range, e.g. to match
        a full year against Shopify; otherwise `days` is used as a trailing window."""
        return await postex.get_dashboard_summary(days=days, from_date=from_date, to_date=to_date)

    @mcp.tool()
    async def list_postex_orders(
        days: int = 30, from_date: Optional[str] = None, to_date: Optional[str] = None,
        bucket: Optional[str] = None, limit: int = 100,
    ) -> dict:
        """Raw PostEx orders for a window, every field PostEx sent (unfiltered) --
        useful for inspecting exact field names PostEx uses for things like
        payment mode or customer phone, which aren't documented anywhere.
        `bucket` optionally filters to one of: delivered, returned, in_transit,
        unbooked, unclassified."""
        fd, td = _default_range(from_date, to_date, days)
        orders, _ = await postex.get_orders_classified(status_id=0, from_date=fd, to_date=td)
        if bucket:
            orders = [o for o in orders if o.get("_bucket") == bucket]
        return {"orders": orders[:limit], "total_matched": len(orders), "from_date": fd, "to_date": td}

    @mcp.tool()
    async def track_postex_order(tracking_number: str) -> dict:
        """Full tracking history/timeline for ONE PostEx order by tracking number
        (booked -> in transit -> delivered/returned, with timestamps if PostEx
        provides them). This is the only way to get a real status history --
        the bulk order list only reports the current status, not the timeline."""
        return await postex.track_order(tracking_number)

    @mcp.tool()
    async def get_postex_return_breakdown_by_city(days: int = 30, from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """Return rate by city for the window -- total orders, returned count,
        and return rate %, sorted by return rate descending. Useful for spotting
        regions with disproportionate COD refusal."""
        fd, td = _default_range(from_date, to_date, days)
        orders, _ = await postex.get_orders_classified(status_id=0, from_date=fd, to_date=td)
        by_city = {}
        for o in orders:
            city = o.get("cityName") or "Unknown"
            row = by_city.setdefault(city, {"total": 0, "returned": 0})
            row["total"] += 1
            if o.get("_bucket") == "returned":
                row["returned"] += 1
        result = [{"city": c, "total": v["total"], "returned": v["returned"], "return_rate": round(v["returned"] / v["total"] * 100, 1) if v["total"] else 0} for c, v in by_city.items()]
        result.sort(key=lambda x: x["return_rate"], reverse=True)
        return {"cities": result, "from_date": fd, "to_date": td}

    @mcp.tool()
    async def get_postex_return_value_breakdown(days: int = 30, from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """Return rate bucketed by COD order value (0-500, 500-1500, 1500-3000,
        3000-5000, 5000+) -- a rough proxy for 'do higher-value orders get
        refused more', since PostEx's order data has no product info to break
        this down by SKU (that would need a Shopify-order-number cross-reference,
        which doesn't exist yet)."""
        fd, td = _default_range(from_date, to_date, days)
        orders, _ = await postex.get_orders_classified(status_id=0, from_date=fd, to_date=td)
        bands = [(0, 500), (500, 1500), (1500, 3000), (3000, 5000), (5000, float("inf"))]

        def band_label(v):
            for lo, hi in bands:
                if lo <= v < hi:
                    return f"{lo}-{hi if hi != float('inf') else '+'}"
            return "unknown"

        by_band = {}
        for o in orders:
            v = float(o.get("invoicePayment", 0) or 0)
            label = band_label(v)
            row = by_band.setdefault(label, {"total": 0, "returned": 0})
            row["total"] += 1
            if o.get("_bucket") == "returned":
                row["returned"] += 1
        result = [{"band_pkr": k, "total": v["total"], "returned": v["returned"], "return_rate": round(v["returned"] / v["total"] * 100, 1) if v["total"] else 0} for k, v in by_band.items()]
        return {"bands": result, "from_date": fd, "to_date": td, "note": "Value bands only -- for a real per-product breakdown use get_product_return_breakdown."}

    @mcp.tool()
    async def get_product_return_breakdown(days: int = 30, from_date: Optional[str] = None, to_date: Optional[str] = None, limit: int = 30) -> dict:
        """Which specific products/variants get returned most, cross-referencing
        PostEx's delivered/returned outcome for each shipment with the Shopify
        order's line items (matched via PostEx's orderRefNumber against the
        Shopify order number). Sorted by return rate, highest first. Not every
        PostEx order can always be matched to a Shopify order -- check
        match_rate in the response; a low match rate means treat the product
        breakdown as a sample, not the full picture."""
        fd, td = _default_range(from_date, to_date, days)
        postex_orders, _ = await postex.get_orders_classified(status_id=0, from_date=fd, to_date=td)
        shopify_orders = await shopify.get_all_orders_range(fd, td, status="any")
        result = compute_product_return_breakdown(postex_orders, shopify_orders)
        result["products"] = result["products"][:limit]
        result["from_date"], result["to_date"] = fd, td
        return result

    @mcp.tool()
    async def get_postex_repeat_customers(days: int = 90, from_date: Optional[str] = None, to_date: Optional[str] = None, min_orders: int = 2) -> dict:
        """Customers (grouped by whatever phone field PostEx returns) with 2+
        orders in the window, and how many of those were returned -- useful for
        spotting repeat COD refusers worth blocking or calling ahead. Depends on
        PostEx's order object actually carrying a usable phone/name field;
        inspect a raw order via list_postex_orders if this comes back empty."""
        fd, td = _default_range(from_date, to_date, days)
        orders, _ = await postex.get_orders_classified(status_id=0, from_date=fd, to_date=td)
        by_phone = {}
        for o in orders:
            phone = o.get("customerPhone") or o.get("phoneNumber") or o.get("customerPhoneNumber") or o.get("consigneePhoneNumber")
            if not phone:
                continue
            row = by_phone.setdefault(phone, {"name": o.get("customerName") or o.get("consigneeName") or "", "orders": 0, "returned": 0})
            row["orders"] += 1
            if o.get("_bucket") == "returned":
                row["returned"] += 1
        repeat = [{"phone": p, **v} for p, v in by_phone.items() if v["orders"] >= min_orders]
        repeat.sort(key=lambda x: x["orders"], reverse=True)
        return {"repeat_customers": repeat, "from_date": fd, "to_date": td, "matched_any_phone_field": len(by_phone) > 0}

    # ── Meta: ads, Facebook, Instagram ──────────────────────────────────────
    # Every write tool below takes `confirm: bool = False`. Called without it
    # (or with it false), nothing happens -- the tool returns a preview of
    # exactly what it would do, so you (via chat) see it before anything is
    # spent or published. Call the SAME tool again with confirm=true to
    # actually execute. This is deliberate and not relaxed by the
    # automation_settings toggles -- those govern a future autonomous agent
    # acting without a human in the loop, not a live conversation like this one.
    def _meta_preview(action, details):
        return {"preview": True, "action": action, "details": details,
                "note": "Nothing was changed. Call again with confirm=true to proceed."}

    @mcp.tool()
    async def meta_check_connection() -> dict:
        """Check the Meta token works, and list which ad accounts, Facebook
        Pages and Instagram account it can see."""
        try:
            return await meta.check_connection()
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def list_ad_campaigns(limit: int = 25) -> dict:
        """List campaigns in the configured ad account with status and budget."""
        try:
            return await meta.list_campaigns(limit=limit)
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def list_ad_sets(campaign_id: Optional[str] = None, limit: int = 25) -> dict:
        """List ad sets, optionally for one campaign. Budgets are in the smallest currency unit (cents)."""
        try:
            return await meta.list_adsets(campaign_id=campaign_id, limit=limit)
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def get_ad_insights(date_preset: str = "last_7d", level: str = "campaign", object_id: Optional[str] = None) -> dict:
        """Ad performance: spend, reach, clicks, CPC, CTR, purchases and ROAS.
        date_preset: today/yesterday/last_3d/last_7d/last_14d/last_30d/this_month/last_month.
        level: account/campaign/adset/ad. object_id defaults to the whole account."""
        try:
            return await meta.get_insights(date_preset=date_preset, level=level, object_id=object_id)
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def set_ad_status(object_id: str, status: str, confirm: bool = False) -> dict:
        """Pause or resume a campaign, ad set or ad (status: ACTIVE or PAUSED).
        Affects live ad spend -- requires confirm=true to actually execute."""
        if status not in ("ACTIVE", "PAUSED"):
            return {"error": "status must be ACTIVE or PAUSED"}
        if not confirm:
            return _meta_preview("Change delivery status", {"object_id": object_id, "status": status})
        try:
            return {"ok": True, "result": await meta.set_status(object_id, status)}
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def set_ad_daily_budget(adset_id: str, amount_minor: int, confirm: bool = False) -> dict:
        """Change an ad set's daily budget. amount_minor is in the smallest
        currency unit (cents for USD, so 1000 = $10.00). Requires confirm=true to execute."""
        if not confirm:
            return _meta_preview("Change daily budget", {"adset_id": adset_id, "new_daily_budget_major_units": amount_minor / 100})
        try:
            return {"ok": True, "result": await meta.set_daily_budget(adset_id, amount_minor)}
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def create_ad_campaign(
        name: str, objective: str, daily_budget_pkr: float, message: str, link: str,
        countries: Optional[list] = None, image_url: Optional[str] = None, cta: str = "SHOP_NOW",
        age_min: int = 18, age_max: int = 65, genders: Optional[list] = None,
        launch_active: bool = False, confirm: bool = False,
    ) -> dict:
        """Build a brand-new ad campaign from scratch (Campaign -> Ad Set ->
        Creative -> Ad). objective is one of: OUTCOME_TRAFFIC, OUTCOME_ENGAGEMENT,
        OUTCOME_SALES, OUTCOME_AWARENESS, OUTCOME_LEADS, OUTCOME_APP_PROMOTION.
        daily_budget_pkr is in whole PKR (e.g. 1000 = PKR 1,000/day). countries
        defaults to ["PK"]. launch_active=False (the default) creates it PAUSED
        so nothing spends until it's resumed from the Ads tab or set_ad_status.

        GUARDRAIL: this always requires confirm=true to actually create
        anything, with NO automation-settings toggle able to skip that --
        unlike pause/resume or budget changes on existing ads, new-campaign
        creation always needs an explicit human approval in the conversation
        first. Call once without confirm to get a full preview, show it to the
        human, and only call again with confirm=true after they explicitly
        approve that exact preview."""
        if not confirm:
            return _meta_preview("Create new ad campaign", {
                "campaign_name": name, "objective": objective, "daily_budget_pkr": daily_budget_pkr,
                "countries": countries or ["PK"], "age_range": f"{age_min}-{age_max}", "genders": genders or "all",
                "ad_message": message, "link": link, "image_url": image_url or "(none)", "call_to_action": cta,
                "launch_status": "ACTIVE immediately" if launch_active else "PAUSED (needs a manual resume to spend)",
            })
        try:
            daily_budget_minor = int(round(daily_budget_pkr * 100))
            result = await meta.create_full_campaign(
                name, objective, daily_budget_minor, countries or ["PK"], message, link,
                image_url=image_url, cta=cta, age_min=age_min, age_max=age_max,
                genders=genders, launch_active=launch_active,
            )
            return {"ok": True, "result": result}
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def list_facebook_posts(limit: int = 10) -> dict:
        """Recent posts on the Facebook Page, including scheduled ones."""
        try:
            return await meta.list_page_posts(limit=limit)
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def post_to_facebook(message: str, image_url: Optional[str] = None, link: Optional[str] = None, schedule_at: Optional[int] = None, confirm: bool = False) -> dict:
        """Publish or schedule a Facebook Page post. schedule_at is a unix
        timestamp in seconds (10 min to 30 days ahead) to schedule instead of
        posting now. Requires confirm=true to execute."""
        if not confirm:
            return _meta_preview(
                "Schedule Facebook post" if schedule_at else "Publish Facebook post NOW",
                {"message": message, "image_url": image_url, "link": link, "schedule_at": schedule_at or "now"},
            )
        try:
            return {"ok": True, "result": await meta.post_to_page(message, image_url=image_url, link=link, schedule_at=schedule_at)}
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def list_instagram_media(limit: int = 12) -> dict:
        """Recent Instagram posts with likes and comment counts."""
        try:
            return await meta.list_ig_media(limit=limit)
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def get_instagram_media_insights(media_id: str) -> dict:
        """Reach, likes, comments, shares and saves for one Instagram post."""
        try:
            return await meta.ig_insights(media_id)
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def list_instagram_comments(media_id: str, limit: int = 25) -> dict:
        """Comments on an Instagram post."""
        try:
            return await meta.list_comments(media_id, limit=limit)
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def reply_to_instagram_comment(comment_id: str, message: str, confirm: bool = False) -> dict:
        """Reply publicly to an Instagram comment. Requires confirm=true to execute."""
        if not confirm:
            return _meta_preview("Reply to Instagram comment", {"comment_id": comment_id, "message": message})
        try:
            return {"ok": True, "result": await meta.reply_to_comment(comment_id, message)}
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def publish_instagram_image(image_url: str, caption: str, confirm: bool = False) -> dict:
        """Publish an image to Instagram now. image_url must be a public JPEG
        URL. Instagram has no native scheduling. Requires confirm=true to execute."""
        if not confirm:
            return _meta_preview("Publish Instagram image NOW", {"image_url": image_url, "caption": caption})
        try:
            return {"ok": True, "result": await meta.publish_ig_image(image_url, caption)}
        except MetaError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def publish_instagram_reel(video_url: str, caption: str, confirm: bool = False) -> dict:
        """Publish a Reel to Instagram now. video_url must be a public MP4 URL.
        Can take up to ~2.5 minutes to process. Requires confirm=true to execute."""
        if not confirm:
            return _meta_preview("Publish Instagram Reel NOW", {"video_url": video_url, "caption": caption})
        try:
            return {"ok": True, "result": await meta.publish_ig_reel(video_url, caption)}
        except MetaError as e:
            return {"error": str(e)}

    # ── Automation autonomy toggles ──────────────────────────────────────────
    @mcp.tool()
    async def get_automation_settings() -> dict:
        """Current autonomy toggles for ads/social actions (ads_pause_resume,
        ads_budget_change, social_publish, comment_reply) -- whether each is
        allowed to run without a human approving first, plus any config like a
        budget ceiling. New ad/campaign creation has no toggle: it always
        requires approval."""
        return await db.get_automation_settings()

    @mcp.tool()
    async def set_automation_setting(key: str, enabled: Optional[bool] = None, config: Optional[dict] = None) -> dict:
        """Turn an autonomy toggle on/off, or update its config (e.g. a budget
        ceiling). key: ads_pause_resume, ads_budget_change, social_publish, or
        comment_reply. WRITE action -- changes what future automation is allowed to do."""
        try:
            return await db.set_automation_setting(key, enabled=enabled, config=config)
        except ValueError as e:
            return {"error": str(e)}

    # ── Accounting: reads ────────────────────────────────────────────────────
    @mcp.tool()
    async def get_pnl(from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """Profit & loss for a date range: revenue (Shopify + custom orders),
        COGS (materials, outsourced production, printer maintenance +
        depreciation), operating expenses by category, courier fees, labor, net
        profit and margin. Cash-basis, not compliance-grade accrual accounting."""
        fd, td = _default_range(from_date, to_date, 30)
        shopify_orders = await shopify.get_all_orders_range(fd, td)
        custom_orders = await db.list_custom_orders(fd, td)
        vendor_purchases = await db.list_vendor_purchases(fd, td)
        outsourced_jobs = await db.list_outsourced_jobs(fd, td)
        expenses = await db.list_expenses(fd, td)
        employee_payments = await db.list_employee_payments(None, fd, td)
        printer_maintenance = await db.list_printer_maintenance(None, fd, td)
        printers = await db.list_printers()
        postex_payouts = await db.list_postex_payouts(fd, td)
        return generate_pnl(
            fd, td, shopify_orders=shopify_orders, custom_orders=custom_orders,
            vendor_purchases=vendor_purchases, outsourced_jobs=outsourced_jobs,
            expenses=expenses, employee_payments=employee_payments,
            printer_maintenance=printer_maintenance, printers=printers, postex_payouts=postex_payouts,
        )

    @mcp.tool()
    async def get_postex_reconciliation(days: int = 30) -> dict:
        """Compares what PostEx says was delivered (should've been paid to you)
        against payout batches you've logged manually via log_postex_payout --
        surfaces any unreconciled gap."""
        summary = await postex.get_dashboard_summary(days=days)
        fd, td = _default_range(None, None, days)
        payouts = await db.list_postex_payouts(fd, td)
        return reconcile_postex(summary, payouts)

    @mcp.tool()
    async def list_vendors() -> dict:
        """All vendors (filament/printer/parts/other suppliers, or print farms used for outsourcing)."""
        return {"vendors": await db.list_vendors()}

    @mcp.tool()
    async def list_vendor_purchases(from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """Logged purchases from vendors (filament, parts, packaging, etc.) in a date range."""
        return {"purchases": await db.list_vendor_purchases(from_date, to_date)}

    @mcp.tool()
    async def list_printers() -> dict:
        """All 3D printers with current book value / accumulated depreciation attached."""
        printers = await db.list_printers()
        for p in printers:
            p["_depreciation"] = straight_line_depreciation(p)
        return {"printers": printers}

    @mcp.tool()
    async def list_printer_maintenance(printer_id: Optional[str] = None, from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """Logged printer maintenance events, optionally for one printer."""
        return {"logs": await db.list_printer_maintenance(printer_id, from_date, to_date)}

    @mcp.tool()
    async def list_custom_orders(from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """Manually-entered custom orders (outside Shopify) in a date range."""
        return {"orders": await db.list_custom_orders(from_date, to_date)}

    @mcp.tool()
    async def list_outsourced_jobs(from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """Production jobs outsourced to other print farms in a date range."""
        return {"jobs": await db.list_outsourced_jobs(from_date, to_date)}

    @mcp.tool()
    async def list_expenses(from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """Logged operating expenses (rent, utilities, ads, etc.) in a date range."""
        return {"expenses": await db.list_expenses(from_date, to_date)}

    @mcp.tool()
    async def list_employees() -> dict:
        """All employees on record (name, role, payment type/rate)."""
        return {"employees": await db.list_employees()}

    @mcp.tool()
    async def list_employee_payments(employee_id: Optional[str] = None, from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """Logged employee payments, optionally for one employee, in a date range."""
        return {"payments": await db.list_employee_payments(employee_id, from_date, to_date)}

    @mcp.tool()
    async def list_cash_accounts() -> dict:
        """Cash/bank accounts with current computed balance (opening balance + logged transactions)."""
        accounts = await db.list_cash_accounts()
        for a in accounts:
            txns = await db.list_cash_transactions(a["id"])
            a["_balance"] = calc_cash_balance(a, txns)
        return {"accounts": accounts}

    @mcp.tool()
    async def list_cash_transactions(account_id: Optional[str] = None, from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """Cash transactions (in/out), optionally for one account, in a date range."""
        return {"transactions": await db.list_cash_transactions(account_id, from_date, to_date)}

    @mcp.tool()
    async def list_postex_payouts(from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """Logged PostEx settlement/payout batches (gross, fee deducted, net) in a date range."""
        return {"payouts": await db.list_postex_payouts(from_date, to_date)}

    # ── Accounting: writes (create/update only -- no deletes, see module docstring) ──
    @mcp.tool()
    async def add_vendor(name: str, type: str = "other", contact: str = "", notes: str = "") -> dict:
        """Add a new vendor (filament/printer/parts supplier or print farm). WRITE action."""
        row = await db.create_vendor({"name": name, "type": type, "contact": contact, "notes": notes})
        return {"ok": True, "vendor": row}

    @mcp.tool()
    async def add_vendor_purchase(
        vendor_id: Optional[str] = None, category: str = "other", description: str = "",
        quantity: Optional[float] = None, unit: str = "", unit_cost: Optional[float] = None,
        total_cost: Optional[float] = None, purchase_date: Optional[str] = None,
        payment_status: str = "paid", amount_paid: Optional[float] = None, receipt_note: str = "",
    ) -> dict:
        """Log a purchase from a vendor (filament, packaging, parts, etc.). WRITE action."""
        qty = float(quantity or 0)
        uc = float(unit_cost or 0)
        tc = float(total_cost or (qty * uc) or 0)
        paid = float(amount_paid if amount_paid is not None else (tc if payment_status == "paid" else 0))
        row = await db.create_vendor_purchase({
            "vendor_id": vendor_id, "category": category, "description": description,
            "quantity": qty or None, "unit": unit, "unit_cost": uc or None, "total_cost": tc,
            "purchase_date": purchase_date or datetime.utcnow().date().isoformat(),
            "payment_status": payment_status, "amount_paid": paid, "amount_due": round(tc - paid, 2),
            "receipt_note": receipt_note,
        })
        return {"ok": True, "purchase": row}

    @mcp.tool()
    async def add_printer(
        name: str, model: str = "", vendor_id: Optional[str] = None, purchase_cost: float = 0,
        purchase_date: Optional[str] = None, useful_life_years: float = 3, salvage_value: float = 0,
    ) -> dict:
        """Add a new 3D printer for depreciation/maintenance tracking. WRITE action."""
        row = await db.create_printer({
            "name": name, "model": model, "vendor_id": vendor_id, "purchase_cost": purchase_cost,
            "purchase_date": purchase_date, "useful_life_years": useful_life_years,
            "salvage_value": salvage_value, "status": "active",
        })
        return {"ok": True, "printer": row}

    @mcp.tool()
    async def log_printer_usage(printer_id: str, hours: float, log_date: Optional[str] = None, notes: str = "") -> dict:
        """Log printer running hours. WRITE action."""
        total = await db.log_printer_usage(printer_id, hours, log_date, notes)
        return {"ok": True, "total_hours": total}

    @mcp.tool()
    async def log_printer_maintenance(printer_id: str, type: str = "routine", description: str = "", cost: float = 0, log_date: Optional[str] = None) -> dict:
        """Log a printer maintenance event and its cost. WRITE action."""
        row = await db.log_printer_maintenance({
            "printer_id": printer_id, "log_date": log_date or datetime.utcnow().date().isoformat(),
            "type": type, "description": description, "cost": cost,
        })
        return {"ok": True, "log": row}

    @mcp.tool()
    async def add_custom_order(
        customer_name: str, contact: str = "", description: str = "", sale_price: float = 0,
        tax_amount: float = 0, cost_estimate: float = 0, payment_status: str = "unpaid",
        amount_paid: float = 0, production_type: str = "in_house", status: str = "quoted",
        order_date: Optional[str] = None, delivery_date: Optional[str] = None, notes: str = "",
    ) -> dict:
        """Log a manually-entered custom order (outside Shopify). WRITE action."""
        row = await db.create_custom_order({
            "customer_name": customer_name, "contact": contact, "description": description,
            "sale_price": sale_price, "tax_amount": tax_amount, "cost_estimate": cost_estimate,
            "payment_status": payment_status, "amount_paid": amount_paid, "production_type": production_type,
            "status": status, "order_date": order_date or datetime.utcnow().date().isoformat(),
            "delivery_date": delivery_date, "notes": notes,
        })
        return {"ok": True, "order": row}

    @mcp.tool()
    async def update_custom_order(order_id: str, status: Optional[str] = None, payment_status: Optional[str] = None, amount_paid: Optional[float] = None) -> dict:
        """Update a custom order's status/payment fields. WRITE action."""
        data = {k: v for k, v in {"status": status, "payment_status": payment_status, "amount_paid": amount_paid}.items() if v is not None}
        await db.update_custom_order(order_id, data)
        return {"ok": True, "updated": data}

    @mcp.tool()
    async def add_outsourced_job(
        vendor_id: Optional[str] = None, order_ref: str = "", description: str = "", quantity: int = 1,
        cost_charged: float = 0, date_sent: Optional[str] = None, expected_return_date: Optional[str] = None,
        status: str = "sent", notes: str = "",
    ) -> dict:
        """Log a production job outsourced to another print farm. WRITE action."""
        row = await db.create_outsourced_job({
            "vendor_id": vendor_id, "order_ref": order_ref, "description": description, "quantity": quantity,
            "cost_charged": cost_charged, "date_sent": date_sent or datetime.utcnow().date().isoformat(),
            "expected_return_date": expected_return_date, "status": status, "notes": notes,
        })
        return {"ok": True, "job": row}

    @mcp.tool()
    async def add_expense(category: str, description: str = "", amount: float = 0, expense_date: Optional[str] = None, recurring: bool = False, recurrence_period: Optional[str] = None, vendor_id: Optional[str] = None) -> dict:
        """Log an operating expense (rent, utilities, ads, etc.). WRITE action."""
        row = await db.create_expense({
            "category": category, "description": description, "amount": amount,
            "expense_date": expense_date or datetime.utcnow().date().isoformat(),
            "recurring": recurring, "recurrence_period": recurrence_period, "vendor_id": vendor_id,
        })
        return {"ok": True, "expense": row}

    @mcp.tool()
    async def add_employee_payment(employee_id: str, amount: float, payment_date: Optional[str] = None, period_covered: str = "", notes: str = "") -> dict:
        """Log a payment made to an employee. WRITE action."""
        row = await db.create_employee_payment({
            "employee_id": employee_id, "amount": amount,
            "payment_date": payment_date or datetime.utcnow().date().isoformat(),
            "period_covered": period_covered, "notes": notes,
        })
        return {"ok": True, "payment": row}

    @mcp.tool()
    async def add_cash_transaction(account_id: str, direction: str, amount: float, category: str = "other", reference: str = "", txn_date: Optional[str] = None, notes: str = "") -> dict:
        """Log a cash-account transaction. `direction` is 'in' or 'out'. WRITE action."""
        row = await db.create_cash_transaction({
            "account_id": account_id, "direction": direction, "amount": amount, "category": category,
            "reference": reference, "txn_date": txn_date or datetime.utcnow().date().isoformat(), "notes": notes,
        })
        return {"ok": True, "transaction": row}

    @mcp.tool()
    async def log_postex_payout(payout_date: Optional[str] = None, gross_amount: float = 0, fee_deducted: float = 0, net_amount: Optional[float] = None, order_count: int = 0, notes: str = "") -> dict:
        """Log a PostEx settlement/payout batch, for reconciliation against PostEx's own delivered-COD totals. WRITE action."""
        net = net_amount if net_amount is not None else (gross_amount - fee_deducted)
        row = await db.create_postex_payout({
            "payout_date": payout_date or datetime.utcnow().date().isoformat(),
            "gross_amount": gross_amount, "fee_deducted": fee_deducted, "net_amount": net,
            "order_count": order_count, "notes": notes,
        })
        return {"ok": True, "payout": row}

    # ── Filament inventory ───────────────────────────────────────────────────
    @mcp.tool()
    async def list_filament_types() -> dict:
        """All stocked filament types (material + colour), e.g. 'PLA Black',
        'PETG Red'. Use this to find a filament_type_id before logging a
        purchase or resolving a needs-review item."""
        return {"types": await db.list_filament_types()}

    @mcp.tool()
    async def add_filament_type(color: str, material: str = "PLA", reorder_threshold_grams: float = 200, spool_weight_grams: float = 1000, vendor_id: Optional[str] = None, notes: str = "") -> dict:
        """Register a new filament type you stock (e.g. material='PLA',
        color='Black'). WRITE action -- but low risk, just a catalog entry,
        no confirm needed."""
        row = await db.create_filament_type({
            "material": material, "color": color,
            "reorder_threshold_grams": reorder_threshold_grams, "spool_weight_grams": spool_weight_grams,
            "vendor_id": vendor_id, "notes": notes,
        })
        return {"ok": True, "filament_type": row}

    @mcp.tool()
    async def log_filament_purchase(filament_type_id: str, grams: float, cost: float = 0, vendor_id: Optional[str] = None, purchase_date: Optional[str] = None, notes: str = "") -> dict:
        """Log filament you just bought (e.g. a 1kg spool -> grams=1000),
        adding it to that type's stock. WRITE action, no confirm needed --
        this only adds stock, it can't remove or damage anything."""
        row = await db.create_filament_purchase({
            "filament_type_id": filament_type_id, "grams": grams, "cost": cost,
            "vendor_id": vendor_id, "purchase_date": purchase_date or datetime.utcnow().date().isoformat(),
            "notes": notes,
        })
        return {"ok": True, "purchase": row}

    @mcp.tool()
    async def get_filament_status(lead_time_days: int = 14) -> dict:
        """Current stock, recent daily usage, projected days left, and a
        reorder suggestion (in whole spools) for every filament type --
        'when will I run out of X and how much should I buy'."""
        types = await db.list_filament_types()
        purchases = await db.list_filament_purchases()
        consumption = await db.list_filament_consumption(days=60)
        return {"status": compute_filament_status(types, purchases, consumption, lead_time_days=lead_time_days)}

    @mcp.tool()
    async def list_filament_needs_review() -> dict:
        """Order line items that couldn't be auto-matched to a filament type
        (multi-colour product, unstocked colour, no colour variant at all) --
        each needs a one-time manual filament assignment via
        resolve_filament_consumption."""
        return {"items": await db.list_filament_needs_review()}

    @mcp.tool()
    async def resolve_filament_consumption(consumption_id: str, filament_type_id: str, grams: Optional[float] = None, remember_for_product: bool = False) -> dict:
        """Manually assign a needs-review line item to the correct filament
        type (optionally overriding the grams used). Set remember_for_product
        =true to also save this as that product/variant's mapping so future
        orders auto-match without needing review again. WRITE action, no
        confirm needed -- this only corrects bookkeeping, not a live system."""
        items = await db.list_filament_needs_review()
        item = next((i for i in items if i["id"] == consumption_id), None)
        await db.resolve_filament_consumption(consumption_id, filament_type_id, grams=grams)
        if remember_for_product and item and item.get("product_id"):
            await db.upsert_product_filament_link(
                item["product_id"], item.get("variant_id"), filament_type_id,
                grams if grams is not None else (item.get("grams") or 50),
            )
        return {"ok": True}

    # ── Reminders / follow-ups ───────────────────────────────────────────────
    @mcp.tool()
    async def create_reminder(due_at: str, message: str, context: Optional[dict] = None) -> dict:
        """Set a follow-up reminder for yourself to check back on something and
        tell the human about it later (e.g. "check this ad's performance
        tomorrow at 9am and report back"). due_at is an ISO date/time
        (YYYY-MM-DDTHH:MM:SS, Pakistan local time). When it's due, it's pushed
        to the human over whatever notification channels are configured
        (Telegram/email) -- you don't need to still be in this conversation
        for it to fire. WRITE action, no confirm needed -- it's just a note,
        not a live system action."""
        dt = datetime.fromisoformat(due_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=PKT)  # bare date/times are assumed Pakistan local, not UTC
        row = await db.create_reminder(dt.isoformat(), message, context=context)
        return {"ok": True, "reminder": row}

    @mcp.tool()
    async def list_reminders(status: Optional[str] = None) -> dict:
        """Your pending/sent/cancelled reminders. status filters to one of
        those three if given, otherwise all of them."""
        return {"reminders": await db.list_reminders(status=status)}

    @mcp.tool()
    async def cancel_reminder(reminder_id: str) -> dict:
        """Cancel a pending reminder that's no longer needed. WRITE action, no confirm needed."""
        await db.cancel_reminder(reminder_id)
        return {"ok": True}

    # ── Notifications ────────────────────────────────────────────────────────
    @mcp.tool()
    async def send_test_notification(message: str = "Test notification from TDPK Manager.") -> dict:
        """Sends a test push to every configured phone channel (Telegram/
        email) right now, so the human can confirm their setup actually
        works. Reports which channels are configured and whether each send
        succeeded. WRITE action (sends a real message), no confirm needed --
        it's explicitly a test, harmless to send."""
        return await notify_all("TDPK Manager test", message, db=db)

    return mcp
