"""COD risk, digest helpers, per-product costing, the SEO approval queue and
login hardening."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from core import risk, digest, costing, seo_queue, auth
from core.auth import verify_session


# ── COD risk ────────────────────────────────────────────────────────────────

def postex(phone, city, bucket):
    return {"customerPhone": phone, "cityName": city, "_bucket": bucket}

HISTORY = risk.build_history(
    [postex("03001234567", "Karachi", "returned"), postex("03111111111", "Lahore", "delivered")]
    + [postex(f"0300000000{i}", "Jhang", "returned" if i < 3 else "delivered") for i in range(6)]
    + [postex(f"0321000000{i}", "Lahore", "delivered") for i in range(10)])


def shop_order(phone, city, total, orders_count=3):
    return {"id": 1, "name": "#1", "phone": phone, "total_price": str(total),
            "shipping_address": {"city": city}, "customer": {"orders_count": orders_count}}


def test_phone_normalisation():
    assert risk.normalize_phone("+92 300 1234567") == risk.normalize_phone("0300-1234567") == "3001234567"
    assert risk.normalize_phone("123") is None


def test_previous_refuser_is_high_risk():
    a = risk.assess(shop_order("+923001234567", "Karachi", 1500), HISTORY)
    assert a["level"] == "high" and "refused 1 earlier parcel" in a["reasons"][0]


def test_good_customer_is_low_risk():
    assert risk.assess(shop_order("03111111111", "Lahore", 6000), HISTORY)["level"] == "low"


def test_high_refusal_city_plus_value():
    a = risk.assess(shop_order("03339999999", "Jhang", 5500), HISTORY)
    assert a["level"] == "high" and any("Jhang return rate 50%" in r for r in a["reasons"])


def test_awaiting_booking_filter():
    orders = [{"id": 1, "fulfillment_status": None}, {"id": 2, "fulfillment_status": "fulfilled"},
              {"id": 3, "fulfillment_status": None, "cancelled_at": "x"}]
    assert [o["id"] for o in risk.awaiting_booking(orders)] == [1]


# ── Digest helpers ──────────────────────────────────────────────────────────

def test_stuck_parcels():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    orders = [{"_bucket": "in_transit", "transactionDate": "2026-10-20T10:00:00.000+0500", "orderRefNumber": "#1800", "cityName": "Quetta", "transactionStatus": "Booked"},
              {"_bucket": "in_transit", "transactionDate": "2026-10-30T10:00:00.000+0500", "orderRefNumber": "#1840"},
              {"_bucket": "delivered", "transactionDate": "2026-10-01T10:00:00.000+0500", "orderRefNumber": "#1700"}]
    stuck = digest.stuck_parcels(orders, now=now)
    assert [p["ref"] for p in stuck] == ["#1800"] and stuck[0]["days"] == 11  # 20 Oct 05:00 UTC -> 1 Nov 00:00 UTC


def test_bambu_token_warning():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    s = lambda days: {"bambu_access_token": "x", "bambu_token_obtained_at": (now - timedelta(days=days)).isoformat()}
    assert digest.bambu_token_warning(s(10), True, now) is None
    assert "expires in about 5 day" in digest.bambu_token_warning(s(85), True, now)
    assert "probably expired" in digest.bambu_token_warning(s(95), True, now)
    assert "aren't connected" in digest.bambu_token_warning({}, True, now)
    assert digest.bambu_token_warning({}, False, now) is None


# ── Costing ─────────────────────────────────────────────────────────────────

def test_filament_cost_per_gram_from_purchases():
    assert costing.filament_cost_per_gram([{"grams": 1000, "cost": 4500}, {"grams": 2000, "cost": 9000}, {"grams": 500, "cost": 0}]) == 4.5
    assert costing.filament_cost_per_gram([]) == costing.FALLBACK_COST_PER_GRAM


def test_variant_margins_real_and_estimated():
    product = {"id": 1, "variants": [{"id": 10, "title": "Small", "price": "2500"}, {"id": 11, "title": "Large", "price": "6000"},
                                     {"id": 12, "title": "Huge", "price": "9000"}]}
    index = costing.links_index([{"product_id": 1, "variant_id": 10, "grams_per_unit": 120},
                                 {"product_id": 1, "variant_id": None, "grams_per_unit": 400}])
    rows = {r["variant"]: r for r in costing.variant_margins(product, index, 4.5, costing.COST_CONFIG)}
    assert rows["Small"]["filament_cost"] == 540 and rows["Small"]["total_cost"] == 1725 and rows["Small"]["margin_pct"] == 31.0
    assert rows["Large"]["grams"] == 400 and rows["Large"]["basis"] == "real grams"
    health = main._calc_price_health(product, (index, 4.5))
    assert health["basis"] == "real grams" and "~" not in health["label"]
    assert "~" in main._calc_price_health(product)["label"]


# ── SEO approval queue ──────────────────────────────────────────────────────

def test_resource_for_targets():
    assert seo_queue.resource_for("batman-signal-lamp") ["kind"] == "product"
    assert seo_queue.resource_for("batman-collection")["handle"] == "batman"
    assert seo_queue.resource_for("home")["kind"] is None
    assert seo_queue.can_apply({"kind": "low_ctr"}, seo_queue.resource_for("lotr-collection"))
    assert not seo_queue.can_apply({"kind": "slow"}, seo_queue.resource_for("lotr-collection"))
    assert not seo_queue.can_apply({"kind": "low_ctr"}, seo_queue.resource_for("home"))


def test_collection_falls_back_to_smart_only_on_404():
    class Shop:
        def __init__(self, first_status): self.first_status, self.paths = first_status, []
        async def _put(self, path, data):
            self.paths.append(path)
            if path.startswith("/custom_collections") and self.first_status:
                raise httpx.HTTPStatusError("x", request=httpx.Request("PUT", "https://x"), response=httpx.Response(self.first_status))
            return {}
    res = {"kind": "collection"}
    s = Shop(404); asyncio.run(seo_queue.apply_copy(s, res, 5, "T", "D"))
    assert s.paths == ["/custom_collections/5.json", "/smart_collections/5.json"]
    s = Shop(422)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(seo_queue.apply_copy(s, res, 5, "T", "D"))
    assert s.paths == ["/custom_collections/5.json"]


def test_draft_copy_parses_and_trims(monkeypatch):
    async def fake_generate(prompt):
        assert "batman lamp" in prompt and "no invented prices" in prompt
        return '```json\n{"title": "' + "T" * 80 + '", "description": "Desc"}\n```', "m1"
    monkeypatch.setattr(seo_queue.gemini_fallback, "generate_text", fake_generate)
    s = {"kind": "striking_distance", "detail": {"query": "batman lamp", "position": 8.4}}
    res = seo_queue.resource_for("batman-signal-lamp")
    out = asyncio.run(seo_queue.draft_copy(s, res, {"name": "Lamp", "title": "Old", "description": "Old d"}))
    assert len(out["title"]) == 60 and out["description"] == "Desc" and out["model"] == "m1"


class SeoDB:
    def __init__(self):
        self.row = {"id": "s1", "page_key": "batman-signal-lamp", "kind": "low_ctr", "status": "draft",
                    "detail": {"position": 5.2, "ctr": 0.002}, "suggestion": "x",
                    "proposed_title": "New title", "proposed_description": "New description"}
        self.updates, self.notes = [], []
    async def get_seo_suggestion(self, sid): return self.row if sid == "s1" else None
    async def update_seo_suggestion(self, sid, data): self.updates.append(data); self.row.update(data)
    async def add_notification(self, *a): self.notes.append(a)
    async def list_seo_suggestions(self, status=None): return [self.row]
    async def latest_seo_run(self): return None


@pytest.fixture
def seo_app(monkeypatch):
    db, applied = SeoDB(), []
    async def current(shop, res): return {"id": 77, "name": "Lamp", "title": "Old title", "description": "Old description"}
    async def apply(shop, res, oid, title, description): applied.append((res["kind"], oid, title, description))
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(seo_queue, "current_seo", current)
    monkeypatch.setattr(seo_queue, "apply_copy", apply)
    main.app.dependency_overrides[verify_session] = lambda: True
    yield TestClient(main.app), db, applied
    main.app.dependency_overrides.clear()


def test_seo_approve_previews_then_applies(seo_app):
    client, db, applied = seo_app
    r = client.post("/api/seo/suggestions/s1/approve", json={}).json()
    assert r["preview"] and r["current"]["title"] == "Old title" and r["new"]["title"] == "New title" and applied == []
    r = client.post("/api/seo/suggestions/s1/approve", json={"confirm": True}).json()
    assert r["applied"] and applied == [("product", 77, "New title", "New description")]
    assert db.row["status"] == "done" and db.row["previous_title"] == "Old title"
    assert client.post("/api/seo/suggestions/s1/approve", json={"confirm": True}).status_code == 400  # not twice


def test_seo_approve_rejects_overlong_copy(seo_app):
    client, _, applied = seo_app
    r = client.post("/api/seo/suggestions/s1/approve", json={"title": "x" * 61, "description": "d", "confirm": True})
    assert r.status_code == 400 and applied == []


def test_seo_manual_suggestion_is_only_marked_done(seo_app):
    client, db, applied = seo_app
    db.row["kind"] = "not_indexed"
    r = client.post("/api/seo/suggestions/s1/approve", json={"confirm": True}).json()
    assert r == {"ok": True, "applied": False} and applied == [] and db.row["status"] == "done"


# ── Login hardening ─────────────────────────────────────────────────────────

def test_pin_hash_new_and_legacy(monkeypatch):
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)
    monkeypatch.setenv("PIN_HASH", auth.hash_pin("4321"))
    assert os.environ["PIN_HASH"].startswith("pbkdf2$") and auth.verify_pin("4321") and not auth.verify_pin("1234")
    import hashlib
    monkeypatch.setenv("PIN_HASH", "abc:" + hashlib.sha256(b"abc4321").hexdigest())
    assert auth.verify_pin("4321") and not auth.verify_pin("4322")


def test_login_lockout(monkeypatch):
    monkeypatch.setattr(auth, "_failed_logins", {})
    ip = "1.2.3.4"
    for _ in range(auth.MAX_FAILED_LOGINS - 1):
        auth.record_failed_login(ip)
    assert auth.login_locked_for(ip) == 0
    auth.record_failed_login(ip)
    assert auth.login_locked_for(ip) > 0
    assert auth.login_locked_for("5.6.7.8") == 0
    auth.clear_failed_logins(ip)
    assert auth.login_locked_for(ip) == 0


def test_login_route_returns_429_when_locked(monkeypatch):
    monkeypatch.setattr(auth, "_failed_logins", {"testclient": [__import__("time").time()] * auth.MAX_FAILED_LOGINS})
    r = TestClient(main.app).post("/api/auth/login", json={"pin": "0000", "totp": "000000"})
    assert r.status_code == 429
