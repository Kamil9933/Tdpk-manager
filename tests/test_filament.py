"""Filament deduction for orders fulfilled anywhere, and colour matching
against this store's real variant names."""
import asyncio

import pytest

from core import filament

COLORS = ["Black", "Blue", "Green", "Grey", "Purple", "Red", "White", "Yellow"]


class DB:
    def __init__(self, purchases=True):
        self.settings, self.consumption, self.has_purchases = {}, [], purchases
    async def get_cost_settings(self): return dict(self.settings)
    async def update_cost_settings(self, d): self.settings.update({k: str(v) for k, v in d.items()})
    async def list_filament_purchases(self):
        return [{"created_at": "2026-10-17T09:00:00+00:00"}, {"created_at": "2026-10-17T09:05:00+00:00"}] if self.has_purchases else []
    async def list_filament_types(self): return [{"id": c, "color": c, "active": True} for c in COLORS]
    async def get_product_filament_link(self, *a): return None
    async def order_has_filament_consumption(self, oid): return any(c["shopify_order_id"] == oid for c in self.consumption)
    async def create_filament_consumption(self, row): self.consumption.append(row)


def order(oid, fulfilled_at, *variant_titles, cancelled=False):
    return {"id": oid, "name": f"#{oid}", "cancelled_at": "x" if cancelled else None,
            "fulfillments": [{"created_at": fulfilled_at}],
            "line_items": [{"product_id": 1, "variant_id": i, "quantity": 1, "title": "P", "variant_title": vt}
                           for i, vt in enumerate(variant_titles)]}


class Shop:
    def __init__(self, orders): self.orders, self.seen_since = orders, []
    async def get_shipped_orders_updated_since(self, since): self.seen_since.append(since); return self.orders


@pytest.mark.parametrize("variant_title,expected", [
    ("White", "White"),
    ("Medium ( 21 cm tall) / Multicolors(choose and then come inbox)", None),
    ("8.6cm x 8.1cm x 3cm / Green", "Green"),
    ("White / Large = 42 cm x 12.9cm x 3mm", "White"),
    ("Small =6.6 x 4.3 x 8.1     cm / Black", "Black"),
    ("Gold", None), ("Large / Mount Doom", None), (None, None),
])
def test_colour_matching_real_variant_names(variant_title, expected):
    tid, _, _ = asyncio.run(filament.match_filament_for_line_item(DB(), {"product_id": 1, "variant_id": 2, "variant_title": variant_title}))
    assert tid == expected


def test_sync_starts_at_stock_count_and_never_double_counts():
    db = DB()
    shop = Shop([order(1, "2026-10-20T10:00:00+05:00", "Black", "Medium / Green"),
                 order(2, "2026-10-10T10:00:00+05:00", "Red"),        # shipped before the stock count
                 order(3, "2026-10-25T10:00:00+05:00", "Gold"),        # unstocked colour -> needs review
                 order(4, "2026-10-26T10:00:00+05:00", "Blue", cancelled=True)])
    r = asyncio.run(filament.sync_fulfilled_orders(db, shop))
    assert r == {"orders_logged": 2, "items_needing_review": 1, "before_stock_count": 1}
    assert db.settings["filament_tracking_start"] == "2026-10-17T09:05:00+00:00"
    assert sorted({c["shopify_order_id"] for c in db.consumption}) == [1, 3] and len(db.consumption) == 3

    r2 = asyncio.run(filament.sync_fulfilled_orders(db, shop))
    assert r2["orders_logged"] == 0 and len(db.consumption) == 3
    assert shop.seen_since[1] == db.settings["filament_sync_since"] or shop.seen_since[1] > "2026-10-17"
    assert db.settings["filament_tracking_start"] == "2026-10-17T09:05:00+00:00"


def test_sync_skips_without_purchases():
    assert asyncio.run(filament.sync_fulfilled_orders(DB(purchases=False), Shop([])))["skipped"]
