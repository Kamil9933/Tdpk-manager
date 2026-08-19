import asyncio
import httpx

API_VERSION = "2024-04"

class ShopifyClient:
    def __init__(self, store: str, token: str):
        self.base = f"https://{store}/admin/api/{API_VERSION}"
        self.headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json"
        }

    async def _get(self, path, params=None):
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(f"{self.base}{path}", headers=self.headers, params=params)
            r.raise_for_status()
            return r.json()

    async def _put(self, path, data):
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.put(f"{self.base}{path}", headers=self.headers, json=data)
            r.raise_for_status()
            return r.json()

    async def _post(self, path, data):
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{self.base}{path}", headers=self.headers, json=data)
            r.raise_for_status()
            return r.json()

    async def _delete(self, path):
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.delete(f"{self.base}{path}", headers=self.headers)
            return r.status_code

    async def get_all_products(self, collection_id=None, tag=None, status=None):
        products = []
        params = {"limit": 250}
        if collection_id:
            params["collection_id"] = collection_id
        if tag:
            params["tag"] = tag
        if status:
            params["status"] = status
        while True:
            data = await self._get("/products.json", params)
            batch = data.get("products", [])
            products.extend(batch)
            if len(batch) < 250:
                break
            if batch:
                params["since_id"] = batch[-1]["id"]
            else:
                break
        return products

    async def get_products_by_ids(self, ids: list):
        if not ids:
            return []
        ids_str = ",".join(str(i) for i in ids)
        data = await self._get("/products.json", {"ids": ids_str, "limit": 250})
        return data.get("products", [])

    async def get_collections(self):
        custom = await self._get("/custom_collections.json", {"limit": 250})
        smart = await self._get("/smart_collections.json", {"limit": 250})
        return {"collections": custom.get("custom_collections", []) + smart.get("smart_collections", [])}

    async def get_all_tags(self):
        products = await self.get_all_products()
        tags = set()
        for p in products:
            for tag in p.get("tags", "").split(","):
                t = tag.strip()
                if t:
                    tags.add(t)
        return {"tags": sorted(list(tags))}

    async def get_locations(self):
        data = await self._get("/locations.json")
        return data.get("locations", [])

    async def get_orders(self, status="any", limit=250, since_id=None, created_at_min=None):
        params = {"limit": limit, "status": status}
        if since_id:
            params["since_id"] = since_id
        if created_at_min:
            params["created_at_min"] = created_at_min
        data = await self._get("/orders.json", params)
        return data.get("orders", [])

    async def get_all_orders(self, status="any", days=30):
        from datetime import datetime, timedelta
        orders = []
        params = {"limit": 250, "status": status}
        if days:
            since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            params["created_at_min"] = since
        while True:
            data = await self._get("/orders.json", params)
            batch = data.get("orders", [])
            orders.extend(batch)
            if len(batch) < 250:
                break
            if batch:
                params["since_id"] = batch[-1]["id"]
            else:
                break
        return orders

    async def get_customers(self, limit=250, since_id=None):
        params = {"limit": limit}
        if since_id:
            params["since_id"] = since_id
        data = await self._get("/customers.json", params)
        return data.get("customers", [])

    async def get_all_customers(self):
        customers = []
        params = {"limit": 250}
        while True:
            data = await self._get("/customers.json", params)
            batch = data.get("customers", [])
            customers.extend(batch)
            if len(batch) < 250:
                break
            if batch:
                params["since_id"] = batch[-1]["id"]
            else:
                break
        return customers

    async def get_abandoned_checkouts(self):
        data = await self._get("/checkouts.json", {"limit": 250})
        return data.get("checkouts", [])

    async def update_variant(self, product_id, variant_id, payload):
        return await self._put(f"/products/{product_id}/variants/{variant_id}.json", {"variant": payload})

    async def update_product(self, product_id, payload):
        return await self._put(f"/products/{product_id}.json", {"product": payload})

    async def delete_variant(self, product_id, variant_id):
        return await self._delete(f"/products/{product_id}/variants/{variant_id}.json")

    async def add_variant(self, product_id, payload):
        return await self._post(f"/products/{product_id}/variants.json", {"variant": payload})

    async def adjust_inventory(self, location_id, inventory_item_id, adjustment):
        return await self._post("/inventory_levels/adjust.json", {
            "location_id": location_id,
            "inventory_item_id": inventory_item_id,
            "available_adjustment": adjustment
        })

    async def get_inventory_level(self, inventory_item_id, location_id):
        data = await self._get("/inventory_levels.json", {
            "inventory_item_ids": inventory_item_id,
            "location_ids": location_id
        })
        levels = data.get("inventory_levels", [])
        return levels[0] if levels else None

    async def restore_product(self, product_snapshot: dict):
        pid = product_snapshot["id"]
        await self.update_product(pid, {
            "title": product_snapshot.get("title"),
            "body_html": product_snapshot.get("body_html"),
            "vendor": product_snapshot.get("vendor"),
            "product_type": product_snapshot.get("product_type"),
            "tags": product_snapshot.get("tags"),
            "status": product_snapshot.get("status"),
        })
        for v in product_snapshot.get("variants", []):
            await self.update_variant(pid, v["id"], {
                "price": v.get("price"),
                "compare_at_price": v.get("compare_at_price"),
                "taxable": v.get("taxable"),
                "inventory_policy": v.get("inventory_policy"),
                "inventory_management": v.get("inventory_management"),
                "weight": v.get("weight"),
                "weight_unit": v.get("weight_unit"),
            })
        await asyncio.sleep(0.3)

    async def get_all_orders_range(self, from_date: str, to_date: str, status: str = "any"):
        """Get orders within a custom date range."""
        orders = []
        params = {"limit": 250, "status": status, "created_at_min": f"{from_date}T00:00:00Z", "created_at_max": f"{to_date}T23:59:59Z"}
        while True:
            data = await self._get("/orders.json", params)
            batch = data.get("orders", [])
            orders.extend(batch)
            if len(batch) < 250:
                break
            if batch:
                params["since_id"] = batch[-1]["id"]
            else:
                break
        return orders
