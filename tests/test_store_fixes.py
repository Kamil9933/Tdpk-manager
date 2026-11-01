"""Guardrails, chat rule commands, rollback, queue sync, cron slot claiming,
redaction and Telegram formatting."""
import asyncio
import json
import types
from datetime import datetime, timedelta, timezone

import pytest

import main
from core import rules, notify, operations
from core import supabase_client as sbc
from core.shopify import ShopifyClient
from core.mcp_server import build_mcp_server
from conftest import tool_text


def as_json(result):
    content = result[0] if isinstance(result, tuple) else result
    return json.loads("\n".join(getattr(c, "text", "") for c in content))


class FakeDB:
    def __init__(self, ceiling=None):
        self.settings = {k: {"enabled": False, "config": {}} for k in ("ads_pause_resume", "social_publish", "comment_reply")}
        self.settings["ads_budget_change"] = {"enabled": False, "config": {"ceiling_minor": ceiling}}
        self.set_calls, self.queue, self.printers = [], {}, []
    async def get_automation_settings(self): return self.settings
    async def set_automation_setting(self, key, enabled=None, config=None): self.set_calls.append(key); return self.settings[key]
    async def list_filament_needs_review(self): return [{"id": 1}]
    async def list_printers(self): return self.printers
    async def list_queue_by_printer_status(self, printer, status):
        return [j for j in self.queue.values() if j["printer"] == printer and j["status"] == status]
    async def update_queue_job(self, jid, data): self.queue[jid].update(data)


class FakeShop:
    def __init__(self): self.calls = []
    async def get_orders(self, status="any", limit=250, **k):
        self.calls.append(("recent", limit))
        return [{"id": 1, "name": "#1", "created_at": "2025-01-01T10:00:00+05:00", "total_price": "100"},
                {"id": 2, "name": "#2", "created_at": "2025-03-01T10:00:00+05:00", "total_price": "200"}]
    async def get_all_orders(self, status="any", days=30): self.calls.append(("days", days)); return []
    async def get_all_products(self, **k): return [{"id": 10, "title": "Lamp", "variants": []}]


@pytest.fixture
def mcp():
    db, shop = FakeDB(ceiling=50000), FakeShop()
    return build_mcp_server(shop, None, db, None, resolve_scope=lambda sc: shop.get_all_products()), db, shop


def call(server, name, args):
    return as_json(asyncio.run(server.call_tool(name, args)))


def test_automation_toggle_needs_confirm(mcp):
    server, db, _ = mcp
    assert call(server, "set_automation_setting", {"key": "social_publish", "enabled": True})["preview"]
    assert db.set_calls == []


def test_budget_ceiling_enforced_in_tool(mcp):
    server, _, _ = mcp
    assert "exceeds" in call(server, "set_ad_daily_budget", {"adset_id": "x", "amount_minor": 90000, "confirm": True})["error"]
    assert call(server, "set_ad_daily_budget", {"adset_id": "x", "amount_minor": 1000})["preview"]


def test_remove_color_preview_warns(mcp):
    server, _, _ = mcp
    r = call(server, "run_bulk_catalog_operation", {"operation": "remove_color", "params": {"color": "Red"}})
    assert "DELETES variants" in r["warning"]


def test_list_orders_recent_without_window(mcp):
    server, _, shop = mcp
    r = call(server, "list_orders", {"limit": 10})
    assert shop.calls[-1] == ("recent", 10) and r["orders"][0]["name"] == "#2"
    call(server, "list_orders", {"days": 7})
    assert shop.calls[-1] == ("days", 7)


class FakeMCP:
    def __init__(self, data): self.data = data
    async def call_tool(self, name, args): return tool_text(self.data[name])


RULE_DATA = {
    "list_printers": {"printers": [
        {"name": "bambu1", "live_status": json.dumps({"online": True, "gcode_state": "RUNNING", "mc_percent": 42,
                                                      "mc_remaining_time": 55, "subtask_name": "Batman lamp"}),
         "live_status_updated_at": "2026-11-01T05:55:38"},
        {"name": "bambu2", "live_status": json.dumps({"online": False})}]},
    "get_pnl": {"revenue": {"total": 150000, "shopify": 140000, "custom_orders": 10000}, "cogs": {"total": 40000},
                "opex": {"total": 20000}, "courier_fees": 5000, "labor_cost": 30000, "net_profit": 55000, "margin_pct": 36.7},
    "get_filament_status": {"status": [
        {"material": "PLA", "color": "Black", "in_stock_grams": 350.0, "projected_days_left": 7.2, "needs_reorder": True, "suggested_purchase_spools": 2},
        {"material": "PLA", "color": "Red", "in_stock_grams": 900.0, "projected_days_left": None, "needs_reorder": False}]},
    "get_postex_summary": {"total_orders": 120, "delivered": 90, "delivery_rate": 75.0, "returned": 20, "return_rate": 16.7,
                           "in_transit": 10, "total_cod_value": 300000, "pending_cod": 34029},
}


@pytest.mark.parametrize("cmd,expected", [
    ("printer status", ["RUNNING, 42%, 55 min left (Batman lamp)", "bambu2: offline"]),
    ("pnl", ["- Revenue: Rs 150,000", "- COGS: Rs 40,000", "- Net profit: Rs 55,000"]),
    ("filament", ["PLA Black: 350g, ~7 days left -- reorder 2 spool(s)", "no recent usage", "1 order item(s) still need"]),
    ("postex", ["120 orders", "- Delivered: 90 (75.0%)", "- Pending COD: Rs 34,029"]),
])
def test_rule_commands(cmd, expected):
    out = asyncio.run(rules.try_handle(FakeMCP(RULE_DATA), FakeDB(), "c", cmd))
    assert all(e in out for e in expected), out
    assert "?" not in out and "{" not in out


def test_remove_color_counts_failed_delete(monkeypatch):
    class Shop:
        async def delete_variant(self, pid, vid): return 422 if vid == 2 else 200
    async def no_sleep(*a): return None
    monkeypatch.setattr(operations.asyncio, "sleep", no_sleep)
    prods = [{"id": 1, "options": [{"name": "Color", "position": 1, "id": 9}],
              "variants": [{"id": 1, "option1": "Red"}, {"id": 2, "option1": "Red"}, {"id": 3, "option1": "Blue"}]}]
    assert asyncio.run(operations.op_remove_color(prods, {"color": "red"}, Shop())) == {"changed": 1, "errors": 1, "total_products": 1}


@pytest.fixture
def shop_client(monkeypatch):
    sc, calls = ShopifyClient("x.myshopify.com", token="t"), []
    current = {"product": {"id": 1, "variants": [{"id": 3, "option1": "Blue"}, {"id": 99, "option1": "Green"}]}}
    async def _get(path, params=None):
        return {"locations": [{"id": 777, "active": True}]} if path.startswith("/locations") else current
    async def _put(path, data): calls.append(("PUT", path)); return {}
    async def _post(path, data):
        calls.append(("POST", path, data))
        return {"variant": {"id": 500, "inventory_item_id": 5000}} if "variants" in path else {}
    async def _delete(path): calls.append(("DELETE", path)); return 200
    sc._get, sc._put, sc._post, sc._delete = _get, _put, _post, _delete
    async def no_sleep(*a): return None
    monkeypatch.setattr("core.shopify.asyncio.sleep", no_sleep)
    return sc, calls


SNAP = {"id": 1, "title": "Lamp", "variants": [
    {"id": 1, "option1": "Red", "price": "1000", "inventory_management": "shopify", "inventory_quantity": 4, "inventory_item_id": 11},
    {"id": 3, "option1": "Blue", "price": "1000", "inventory_management": "shopify", "inventory_quantity": 6, "inventory_item_id": 33}]}


def test_rollback_recreates_deleted_variant_with_stock(shop_client):
    sc, calls = shop_client
    r = asyncio.run(sc.restore_product(SNAP, operation="remove_color"))
    posts = [c for c in calls if c[0] == "POST"]
    assert r == {"recreated": 1, "removed": 0, "stock_restored": 1, "errors": 0}
    assert posts[0][2]["variant"]["option1"] == "Red"
    assert posts[1][2] == {"location_id": 777, "inventory_item_id": 5000, "available": 4}
    assert not any(c[0] == "DELETE" for c in calls), "a hand-made variant must never be deleted"


def test_rollback_scope_by_operation(shop_client):
    sc, calls = shop_client
    r = asyncio.run(sc.restore_product(SNAP, operation="add_color"))
    assert ("DELETE", "/products/1/variants/99.json") in calls and r["removed"] == 1
    one = {**SNAP, "variants": [SNAP["variants"][1]]}
    assert asyncio.run(sc.restore_product(one, operation="price_percentage"))["stock_restored"] == 0
    assert asyncio.run(sc.restore_product(one, operation="set_stock"))["stock_restored"] == 1


def test_queue_sync_detects_job_change(monkeypatch):
    db = FakeDB()
    db.queue = {"A": {"id": "A", "printer": "bambu1", "status": "printing"}, "B": {"id": "B", "printer": "bambu1", "status": "queued"}}
    db.printers = [{"name": "bambu1", "bambu_dev_id": "d1", "live_status": json.dumps(
        {"gcode_state": "RUNNING", "subtask_name": "job2", "prev_gcode_state": "RUNNING", "prev_subtask_name": "job1"})}]
    monkeypatch.setattr(main, "db", db)
    assert asyncio.run(main._sync_queue_with_printers()) == 2
    assert db.queue["A"]["status"] == "done" and db.queue["B"]["status"] == "printing"
    db.queue = {"A": {"id": "A", "printer": "bambu1", "status": "printing"}}
    db.printers[0]["live_status"] = json.dumps({"gcode_state": "RUNNING", "subtask_name": "job1",
                                                "prev_gcode_state": "RUNNING", "prev_subtask_name": "job1"})
    assert asyncio.run(main._sync_queue_with_printers()) == 0


def test_printers_stale():
    now = datetime.now(timezone.utc)
    assert not main._printers_stale([{"bambu_dev_id": "d", "live_status_updated_at": (now - timedelta(minutes=2)).isoformat()}])
    assert main._printers_stale([{"bambu_dev_id": "d", "live_status_updated_at": (now - timedelta(hours=3)).isoformat()}])
    assert main._printers_stale([{"bambu_dev_id": "d", "live_status_updated_at": None}])


def test_claim_slot_first_caller_wins(monkeypatch):
    inserted = set()
    class Table:
        def insert(self, row): self.row = row; return self
        def execute(self):
            if self.row["slot"] in inserted:
                raise Exception("duplicate key value violates unique constraint")
            inserted.add(self.row["slot"])
            return types.SimpleNamespace(data=[self.row])
    monkeypatch.setattr(sbc, "get_client", lambda: types.SimpleNamespace(table=lambda name: Table()))
    d = sbc.Database()
    assert asyncio.run(d.claim_slot("2026-11-01:09:00")) is True
    assert asyncio.run(d.claim_slot("2026-11-01:09:00")) is False


def test_telegram_table_formatting():
    out = notify.format_for_telegram("Orders:\n\n| Order | Total |\n| :--- | ---: |\n| **#1845** | 1,019 |\n| #1844 | 1,550 |\n\nDone")
    assert out == "Orders:\n\nOrder · Total\n#1845 · 1,019\n#1844 · 1,550\n\nDone"
