"""GET /api/seo/overview (SEO Rankings tab) against a fake data layer:
empty data, a missing seo_rankings table, keywords with gaps, and the
"lower position is better" card logic."""
import asyncio
import datetime as dt
import types

import pytest
from fastapi.testclient import TestClient

import main
from core import seo_overview as O
from core import supabase_client as sbc
from core.auth import verify_session

TODAY = dt.date(2026, 11, 20)
TARGETS = {
    "pages": [{"key": "home", "name": "Home page", "url": "https://thirddimension.pk/"},
              {"key": "lamp", "name": "Batman lamp", "url": "https://thirddimension.pk/products/batman-lamp"}],
    "queries": ["batman lamp", "bat signal lamp", "3d printing pakistan"],
}


def q(query, day, pos, impr=50, clicks=2, page="https://thirddimension.pk/products/batman-lamp", tracked=True):
    return {"query": query, "page_url": page, "window_end": day, "tracked": tracked, "clicks": clicks,
            "impressions": impr, "ctr": clicks / impr if impr else 0, "position": pos}


def pm(key, day, clicks, impr, pos):
    return {"page_key": key, "window_end": day, "clicks": clicks, "impressions": impr, "ctr": 0, "position": pos}


QUERY_ROWS = [
    # batman lamp: 14 -> gap on 11-12/11-13 -> 9 (latest). Two pages on 11-17: the busier one wins.
    q("batman lamp", "2026-11-10", 14.0), q("batman lamp", "2026-11-11", 13.0),
    q("batman lamp", "2026-11-14", 12.0),
    q("batman lamp", "2026-11-17", 30.0, impr=5, page="https://thirddimension.pk/"),
    q("batman lamp", "2026-11-17", 9.0, impr=80),
    # 3d printing pakistan: 25 -> 15 (almost page 1)
    q("3d printing pakistan", "2026-11-10", 25.0, page="https://thirddimension.pk/"),
    q("3d printing pakistan", "2026-11-17", 15.0, page="https://thirddimension.pk/"),
    # untracked queries: one old and falling, one new this week
    q("lord of the rings lamp", "2026-11-10", 6.0, tracked=False), q("lord of the rings lamp", "2026-11-17", 11.0, tracked=False),
    q("one ring lamp pakistan", "2026-11-15", 18.0, impr=12, tracked=False), q("one ring lamp pakistan", "2026-11-17", 16.0, impr=20, tracked=False),
]
PAGE_ROWS = [pm("home", "2026-11-10", 10, 400, 20.0), pm("lamp", "2026-11-10", 5, 100, 10.0),
             pm("home", "2026-11-17", 12, 500, 15.0), pm("lamp", "2026-11-17", 4, 150, 8.0)]
PSI_ROWS = [{"page_key": "home", "captured_at": "2026-11-16T04:40:00+00:00", "strategy": "mobile", "performance": 41, "lcp_ms": 6100},
            {"page_key": "home", "captured_at": "2026-11-17T04:40:00+00:00", "strategy": "mobile", "performance": 47, "lcp_ms": 5200}]
INDEX_ROWS = [{"page_key": "home", "captured_at": "2026-11-15T04:40:00+00:00", "verdict": "NEUTRAL", "coverage_state": "Discovered - currently not indexed"},
              {"page_key": "home", "captured_at": "2026-11-16T04:40:00+00:00", "verdict": "PASS", "coverage_state": "Submitted and indexed"},
              {"page_key": "home", "captured_at": "2026-11-17T04:40:00+00:00", "verdict": "PASS", "coverage_state": "Submitted and indexed"},
              {"page_key": "lamp", "captured_at": "2026-11-17T04:40:00+00:00", "verdict": "NEUTRAL", "coverage_state": "URL is unknown to Google"}]
RANK_ROWS = [{"captured_at": "2026-11-16T04:50:00+00:00", "query": "batman lamp", "device": "mobile", "position": 8, "ranking_url": "u"},
             {"captured_at": "2026-11-17T04:50:00+00:00", "query": "batman lamp", "device": "mobile", "position": 7, "ranking_url": "u"}]


def build(**kw):
    args = dict(targets=TARGETS, last_run={"started_at": "2026-11-20T04:30:00Z", "status": "ok"}, query_rows=QUERY_ROWS,
                page_rows=PAGE_ROWS, psi_rows=PSI_ROWS, index_rows=INDEX_ROWS, ranking_rows=RANK_ROWS, days="30", today=TODAY)
    args.update(kw)
    return O.build_overview(**args)


def test_cards_compare_latest_window_with_week_before():
    c = build()["cards"]
    assert c["clicks"] == {"value": 16, "previous": 15, "better": True}
    assert c["impressions"]["better"] is True
    # position 13.6 vs 18.0: a lower number is better
    assert c["avg_position"]["value"] < c["avg_position"]["previous"] and c["avg_position"]["better"] is True
    assert c["top10"] == {"value": 1, "previous": 0, "better": True}
    assert c["top20"]["value"] == 2 and c["not_ranking"] == {"value": 1, "previous": 1, "better": None}


def test_keyword_rows_with_gaps():
    o = build()
    kw = {k["query"]: k for k in o["keywords"]}
    lamp = kw["batman lamp"]
    assert lamp["position"] == 9.0 and lamp["change"] == 3.0 and lamp["best"] == 9.0
    assert lamp["page_url"].endswith("/batman-lamp") and lamp["impressions"] == 80
    assert lamp["exact"] == {"mobile": 7}
    spark = lamp["spark"]
    assert len(spark) == 30 and spark[-1] == 9.0 and spark[-6] is None and spark[-4] == 12.0  # gaps stay None
    assert kw["3d printing pakistan"]["almost_page_1"] and not lamp["almost_page_1"]
    assert kw["bat signal lamp"]["status"] == "not_showing_yet" and kw["bat signal lamp"]["position"] is None
    assert o["default_keywords"][:2] == ["batman lamp", "3d printing pakistan"]


def test_chart_leaves_gaps_and_keeps_both_sources():
    ch = build()["chart"]
    labels, series = ch["labels"], ch["search_console"]["batman lamp"]
    assert "2026-11-12" not in labels                    # no data anywhere that day
    gap_day = labels.index("2026-11-15")                 # another keyword has data, this one doesn't
    assert series[gap_day] is None
    assert series[labels.index("2026-11-17")] == {"position": 9.0, "impressions": 80, "clicks": 2}
    assert ch["search_console"]["bat signal lamp"] == [None] * len(labels)
    tracker = ch["tracker"]["batman lamp"]["mobile"]
    assert tracker[labels.index("2026-11-16")] == {"position": 8} and tracker[labels.index("2026-11-10")] is None


def test_movers_new_keywords_distribution_pages():
    o = build()
    assert [m["query"] for m in o["movers"]["up"]] == ["3d printing pakistan", "batman lamp"]
    assert o["movers"]["down"][0] == {"query": "lord of the rings lamp", "position": 11.0, "previous": 6.0, "change": -5.0, "tracked": False}
    assert [k["query"] for k in o["new_keywords"]] == ["one ring lamp pakistan"]
    last = o["distribution"][-1]
    assert last["date"] == "2026-11-17" and last["4-10"] == 1 and last["11-20"] == 1
    pages = {p["key"]: p for p in o["pages"]}
    assert pages["home"]["index"] == "Indexed" and pages["home"]["index_since"].startswith("2026-11-16")
    assert pages["lamp"]["index"] == "Unknown to Google"
    assert pages["home"]["speed"] == 47 and pages["home"]["lcp_ms"] == 5200 and pages["home"]["clicks"] == 12
    assert [t["clicks"] for t in o["traffic"]] == [15, 16]


def test_period_filter():
    o = build(days="7")
    assert min(o["chart"]["labels"]) >= "2026-11-13" and o["days"] == "7"
    assert build(days="all")["chart"]["labels"][0] == "2026-11-10"


def test_empty_data():
    o = build(query_rows=[], page_rows=[], psi_rows=[], index_rows=[], ranking_rows=None, last_run=None)
    assert o["has_search_data"] is False and o["rankings_available"] is False
    assert o["chart"]["labels"] == [] and o["movers"] == {"up": [], "down": []} and o["new_keywords"] == []
    assert all(k["status"] == "not_showing_yet" for k in o["keywords"])
    assert o["cards"]["clicks"] == {"value": None, "previous": None, "better": None}
    assert o["cards"]["not_ranking"]["value"] == 3
    assert [p["index"] for p in o["pages"]] == ["Unknown to Google"] * 2


# ── route ──

class FakeDB:
    def __init__(self, tables):
        self.tables = tables
        self.calls = []
    async def seo_history(self, table, columns, order_col, since=None, since_col=None):
        self.calls.append(table)
        return self.tables.get(table)
    async def latest_seo_run(self):
        return None


@pytest.fixture
def client(monkeypatch):
    main.app.dependency_overrides[verify_session] = lambda: True
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_route_without_rankings_table(client, monkeypatch):
    fake = FakeDB({"seo_query_metrics": QUERY_ROWS, "seo_page_metrics": PAGE_ROWS, "seo_pagespeed": [],
                   "seo_index_status": [], "seo_rankings": None})
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main.seo_queue, "load_targets", lambda: TARGETS)
    r = client.get("/api/seo/overview?days=all")
    assert r.status_code == 200
    d = r.json()
    assert d["rankings_available"] is False and d["chart"]["tracker"] == {} and d["has_search_data"]
    assert set(fake.calls) == {"seo_query_metrics", "seo_page_metrics", "seo_pagespeed", "seo_index_status", "seo_rankings"}


def test_route_rejects_bad_days_and_needs_login(client, monkeypatch):
    monkeypatch.setattr(main, "db", FakeDB({}))
    assert client.get("/api/seo/overview?days=365").status_code == 400
    main.app.dependency_overrides.clear()
    assert TestClient(main.app).get("/api/seo/overview").status_code in (401, 403, 307)


def test_history_reader_treats_missing_table_as_none(monkeypatch):
    class Missing:
        def __getattr__(self, name):
            if name == "execute":
                def boom():
                    raise Exception("{'code': 'PGRST205', 'message': \"Could not find the table 'public.seo_rankings' in the schema cache\"}")
                return boom
            return lambda *a, **k: self
    monkeypatch.setattr(sbc, "get_client", lambda: types.SimpleNamespace(table=lambda name: Missing()))
    assert asyncio.run(sbc.Database().seo_history("seo_rankings", "*", "captured_at")) is None


def test_history_reader_pages_past_1000_rows(monkeypatch):
    calls = []
    class Paged:
        def __init__(self): self.rng = None
        def __getattr__(self, name):
            if name == "execute":
                start, end = self.rng
                calls.append(start)
                n = max(0, min(end, 2499) - start + 1)
                return lambda: types.SimpleNamespace(data=[{"i": start + i} for i in range(n)])
            if name == "range":
                def rng(a, b):
                    self.rng = (a, b); return self
                return rng
            return lambda *a, **k: self
    monkeypatch.setattr(sbc, "get_client", lambda: types.SimpleNamespace(table=lambda name: Paged()))
    rows = asyncio.run(sbc.Database().seo_history("seo_query_metrics", "*", "window_end"))
    assert len(rows) == 2500 and calls == [0, 1000, 2000]
