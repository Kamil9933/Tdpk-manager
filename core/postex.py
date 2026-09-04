import os
import httpx
from datetime import datetime, timedelta

POSTEX_BASE = "https://api.postex.pk/services/integration/api/order"

# Exact status strings per PostEx's documented orderStatusID enum (Order Status /
# List Orders APIs). Matching on these exactly -- rather than loose substrings like
# "deliver" -- matters because "Out For Delivery" and "Out For Return" would
# otherwise get miscounted as "Delivered" / "Returned".
#
# "return"-containing statuses are bucketed as returned UNLESS it's the transient
# "out for return" (still in transit) -- broadened from a narrow exact-match set
# after real accounts were seen sending status labels ("Return Booked", "Delivery
# Returned", "RTO", etc.) that a strict match silently missed, which is exactly why
# returns showed as 0 despite PostEx's own portal showing real return activity.
#
# These live at module level (not nested inside a single function) so every
# feature that needs to know "was this order returned/delivered/etc." -- the
# dashboard, the MCP connector's city/customer/value breakdowns -- shares one
# definition instead of drifting out of sync with each other.
DELIVERED_STATUSES = {"delivered"}
IN_TRANSIT_STATUSES = {
    "booked", "postex warehouse", "out for delivery", "picked by postex",
    "en-route to postex warehouse", "out for return",
}
UNBOOKED_STATUSES = {"unbooked"}


def _status(o):
    return str(o.get("transactionStatus", "")).strip().lower()


def _is_returned(status):
    return "return" in status and status not in IN_TRANSIT_STATUSES


def classify_status(o) -> str:
    """Bucket a raw PostEx order dict into one of: delivered, returned,
    in_transit, unbooked, unclassified. `unclassified` means PostEx sent a
    transactionStatus value none of the known buckets recognize -- it should
    show up in status_breakdown so it's never silently invisible."""
    s = _status(o)
    if s in DELIVERED_STATUSES:
        return "delivered"
    if _is_returned(s):
        return "returned"
    if s in IN_TRANSIT_STATUSES:
        return "in_transit"
    if s in UNBOOKED_STATUSES:
        return "unbooked"
    return "unclassified"


class PostexClient:
    def __init__(self, token: str = None):
        self.token = token or os.getenv("POSTEX_TOKEN", "")
        self.headers = {"token": self.token, "Content-Type": "application/json"}

    async def _get(self, path, params=None, extra_headers=None):
        headers = {**self.headers, **(extra_headers or {})}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{POSTEX_BASE}{path}", headers=headers, params=params)
            r.raise_for_status()
            return r.json()

    async def _get_with_body(self, path, data=None):
        # PostEx's own docs specify a JSON request body on several GET endpoints
        # (get-all-order, get-unbooked-orders, track-bulk-order) -- non-standard,
        # but that's what their API expects. httpx.get() has no `json=` kwarg, so
        # this goes through `.request()` directly to attach a body to a GET.
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.request("GET", f"{POSTEX_BASE}{path}", headers=self.headers, json=data or {})
            r.raise_for_status()
            return r.json()

    async def _post(self, path, data=None):
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{POSTEX_BASE}{path}", headers=self.headers, json=data or {})
            r.raise_for_status()
            return r.json()

    async def _put(self, path, data=None):
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.put(f"{POSTEX_BASE}{path}", headers=self.headers, json=data or {})
            r.raise_for_status()
            return r.json()

    async def get_all_orders(self, status_id=0, from_date=None, to_date=None):
        if not from_date:
            from_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")
        # NOTE: the official PDF docs describe this as a GET-with-JSON-body request
        # using orderStatusID/fromDate/toDate, but that returned a live 400 Bad
        # Request from PostEx's actual server. A working real-world integration
        # (github.com/faizan45640/PostEx-MCP-Server) calls this as a plain GET with
        # orderStatusId/startDate/endDate as query params (also duplicated as
        # headers, which PostEx's server appears to tolerate/expect) -- switched to
        # match that confirmed-working shape.
        params = {"orderStatusId": status_id, "startDate": from_date, "endDate": to_date}
        try:
            return await self._get("/v1/get-all-order", params=params, extra_headers={
                "orderStatusId": str(status_id), "startDate": from_date, "endDate": to_date
            })
        except Exception as e:
            return {"dist": [], "error": str(e)}

    async def track_order(self, tracking_number: str):
        try:
            return await self._get(f"/v1/track-order/{tracking_number}")
        except Exception as e:
            return {"error": str(e)}

    async def track_bulk(self, tracking_numbers: list):
        try:
            return await self._get_with_body("/v1/track-bulk-order", {"trackingNumber": tracking_numbers})
        except Exception as e:
            return {"error": str(e)}

    async def get_payment_status(self, tracking_number: str):
        try:
            return await self._get(f"/v1/payment-status/{tracking_number}")
        except Exception as e:
            return {"error": str(e)}

    async def get_order_statuses(self):
        try:
            return await self._get("/v1/get-order-status")
        except Exception as e:
            return {"dist": [], "error": str(e)}

    async def get_orders(self, status_id=None, from_date=None, to_date=None, page=1):
        # Alias used by the /api/postex/orders endpoint. PostEx's API has no
        # pagination param for get-all-order, so `page` is accepted for
        # forward-compat but currently ignored.
        try:
            sid = int(status_id) if status_id not in (None, "", "0") else 0
        except (TypeError, ValueError):
            sid = 0
        return await self.get_all_orders(status_id=sid, from_date=from_date, to_date=to_date)

    async def get_unbooked_orders(self, from_date=None, to_date=None):
        if not from_date:
            from_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")
        try:
            return await self._get("/v2/get-unbooked-orders", params={"startDate": from_date, "endDate": to_date})
        except Exception as e:
            return {"dist": [], "error": str(e)}

    async def cancel_order(self, tracking_number: str):
        try:
            return await self._put("/v1/cancel-order", {"trackingNumber": tracking_number})
        except Exception as e:
            return {"error": str(e)}

    async def get_orders_classified(self, status_id=0, from_date=None, to_date=None):
        """Fetch orders for a window and return them as a flat list of raw PostEx
        order dicts -- every field PostEx actually sent, untouched -- each
        augmented with `_status_raw` (lowercased transactionStatus) and `_bucket`
        (delivered/returned/in_transit/unbooked/unclassified).

        This is the single source of truth get_dashboard_summary and the MCP
        connector's breakdown tools both build on, specifically so status
        classification can't drift out of sync between features the way it did
        before (returns silently showing as 0). Returns (orders, raw_api_response)
        -- the raw response is included so callers can still see api error/status
        fields.
        """
        if not from_date:
            from_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")

        orders_data = await self.get_all_orders(status_id=status_id, from_date=from_date, to_date=to_date)
        raw = orders_data.get("dist", [])

        orders = []
        for item in raw:
            if isinstance(item, dict):
                tr = dict(item.get("trackingResponse", item))
                tr["_status_raw"] = _status(tr)
                tr["_bucket"] = classify_status(tr)
                orders.append(tr)
        return orders, orders_data

    async def get_dashboard_summary(self, days=30, from_date=None, to_date=None):
        # Accepts either a `days` shorthand (default) or an explicit from_date/
        # to_date range -- the explicit range takes priority. Arbitrary ranges
        # matter for lining PostEx data up against a full year of Shopify data,
        # not just the 7/30/60-day presets the dashboard UI offers today.
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")
        if not from_date:
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        orders, orders_data = await self.get_orders_classified(status_id=0, from_date=from_date, to_date=to_date)

        # TEMP DEBUG: log to Render logs so we can see exactly what came back (or
        # didn't) when the dashboard shows unexpected data. Safe to remove once
        # the integration is confirmed working end-to-end across a few windows.
        print(f"[postex] window={from_date}..{to_date} order_count={len(orders)} api_error={orders_data.get('error','')}")

        total = len(orders)
        delivered = sum(1 for o in orders if o["_bucket"] == "delivered")
        returned = sum(1 for o in orders if o["_bucket"] == "returned")
        in_transit = sum(1 for o in orders if o["_bucket"] == "in_transit")
        unbooked = sum(1 for o in orders if o["_bucket"] == "unbooked")

        # Full breakdown of every raw status PostEx actually sent back, with counts --
        # so anything not caught by the buckets above (a status we haven't seen/named yet)
        # is still visible on the dashboard instead of silently vanishing.
        status_breakdown = {}
        for o in orders:
            s = o["_status_raw"] or "(blank)"
            status_breakdown[s] = status_breakdown.get(s, 0) + 1
        status_breakdown = dict(sorted(status_breakdown.items(), key=lambda x: x[1], reverse=True))

        total_cod = sum(float(o.get("invoicePayment", 0) or 0) for o in orders)
        delivered_cod = sum(float(o.get("invoicePayment", 0) or 0) for o in orders if o["_bucket"] == "delivered")
        returned_cod = sum(float(o.get("invoicePayment", 0) or 0) for o in orders if o["_bucket"] == "returned")

        city_counts = {}
        for o in orders:
            city = o.get("cityName") or "Unknown"
            city_counts[city] = city_counts.get(city, 0) + 1
        top_cities = sorted(city_counts.items(), key=lambda x: x[1], reverse=True)[:8]

        return {
            "total_orders": total,
            "delivered": delivered,
            "returned": returned,
            "in_transit": in_transit,
            "unbooked": unbooked,
            "delivery_rate": round((delivered / total * 100) if total else 0, 1),
            "return_rate": round((returned / total * 100) if total else 0, 1),
            "total_cod_value": round(total_cod, 0),
            "delivered_cod": round(delivered_cod, 0),
            "returned_cod_value": round(returned_cod, 0),
            "pending_cod": round(total_cod - delivered_cod, 0),
            "top_cities": [{"city": c, "count": n} for c, n in top_cities],
            "status_breakdown": status_breakdown,
            "orders": orders[:50],
            "from_date": from_date,
            "to_date": to_date,
            "api_status": orders_data.get("statusMessage", ""),
            "api_code": orders_data.get("statusCode", ""),
            "error": orders_data.get("error", ""),
        }
