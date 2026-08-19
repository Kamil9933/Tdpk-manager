import os
import httpx
from datetime import datetime, timedelta

POSTEX_BASE = "https://api.postex.pk/services/integration/api/order"

class PostexClient:
    def __init__(self, token: str = None):
        self.token = token or os.getenv("POSTEX_TOKEN", "")
        self.headers = {"token": self.token, "Content-Type": "application/json"}

    async def _get(self, path, params=None):
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{POSTEX_BASE}{path}", headers=self.headers, params=params)
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
        try:
            return await self._get("/v1/get-all-order", {
                "orderStatusID": status_id,
                "fromDate": from_date,
                "toDate": to_date
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
            return await self._post("/v1/track-bulk-order", {"trackingNumber": tracking_numbers})
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

    async def get_unbooked_orders(self, from_date=None, to_date=None):
        if not from_date:
            from_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")
        try:
            return await self._get("/v2/get-unbooked-orders", {"startDate": from_date, "endDate": to_date})
        except Exception as e:
            return {"dist": [], "error": str(e)}

    async def cancel_order(self, tracking_number: str):
        try:
            return await self._put("/v1/cancel-order", {"trackingNumber": tracking_number})
        except Exception as e:
            return {"error": str(e)}

    async def get_dashboard_summary(self, days=30):
        to_date = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        orders_data = await self.get_all_orders(status_id=0, from_date=from_date, to_date=to_date)
        raw = orders_data.get("dist", [])

        # Extract order objects - API wraps in trackingResponse
        orders = []
        for item in raw:
            if isinstance(item, dict):
                tr = item.get("trackingResponse", item)
                orders.append(tr)

        total = len(orders)
        delivered = sum(1 for o in orders if "deliver" in str(o.get("transactionStatus","")).lower())
        returned = sum(1 for o in orders if "return" in str(o.get("transactionStatus","")).lower())
        in_transit = sum(1 for o in orders if any(k in str(o.get("transactionStatus","")).lower() for k in ["transit","warehouse","picked","route","attempted","delivery"]))
        unbooked = sum(1 for o in orders if "unbook" in str(o.get("transactionStatus","")).lower())

        total_cod = sum(float(o.get("invoicePayment", 0) or 0) for o in orders)
        delivered_cod = sum(float(o.get("invoicePayment", 0) or 0) for o in orders if "deliver" in str(o.get("transactionStatus","")).lower())

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
            "pending_cod": round(total_cod - delivered_cod, 0),
            "top_cities": [{"city": c, "count": n} for c, n in top_cities],
            "orders": orders[:50],
            "api_status": orders_data.get("statusMessage", ""),
            "api_code": orders_data.get("statusCode", ""),
        }
