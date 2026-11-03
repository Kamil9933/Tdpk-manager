"""SEO report script end-to-end against fake Google, PageSpeed, DataForSEO, Supabase and Telegram."""
import os, json, base64, datetime as dt
from conftest import ROOT
import httpx
import pytest
import seo_report as S
import rank_check as R

PSI_KEY = "AIzaPSIKEYsecret_1234567890abcdefgh"
TG_TOKEN = "123456789:AAHtelegramsecretsecretsecret"
TODAY = dt.date(2026, 11, 1)  # current window 23-29 Oct, previous 16-22 Oct

class Fake:
    def __init__(s, psi_fail=False, drafts=None, sites=("sc-domain:thirddimension.pk",), rank_history=None,
                 dfs_post_status=20000, dfs_always_queued=False):
        s.psi_fail, s.drafts, s.sites = psi_fail, drafts or [], sites
        s.inserts, s.patches, s.telegram, s.gsc_bodies = {}, [], [], []
        s.rank_history, s.dfs_post_status, s.dfs_always_queued = rank_history or [], dfs_post_status, dfs_always_queued
        s.dfs_tasks, s.dfs_gets, s.dfs_auth = {}, {}, set()
    def __call__(s, req):
        u, host = str(req.url), req.url.host
        if host == "thirddimension.pk":
            return httpx.Response(404 if "custom-orders" in u else 200, text="<html>")
        if u.endswith("/webmasters/v3/sites"):
            return httpx.Response(200, json={"siteEntry": [{"siteUrl": x, "permissionLevel": "siteRestrictedUser"} for x in s.sites]})
        if "searchAnalytics" in u:
            body = json.loads(req.content); s.gsc_bodies.append(body)
            cur = body["startDate"] == "2026-10-23"
            if body["dimensions"] == ["page"]:
                return httpx.Response(200, json={"rows": [
                    {"keys": ["https://thirddimension.pk/"], "clicks": 30 if cur else 20, "impressions": 900, "ctr": 0.033, "position": 9.0 if cur else 11.0},
                    {"keys": ["https://thirddimension.pk/products/batman-rooftop-signal-lamp"], "clicks": 1, "impressions": 400, "ctr": 0.0025, "position": 5.2},
                    {"keys": ["https://thirddimension.pk/products/other"], "clicks": 99, "impressions": 999, "ctr": 0.1, "position": 2}]})
            return httpx.Response(200, json={"rows": [
                {"keys": ["Batman Lamp", "https://thirddimension.pk/products/batman-rooftop-signal-lamp"], "clicks": 1, "impressions": 150, "ctr": 0.006, "position": 8.4 if cur else 10.1},
                {"keys": ["3d printing pakistan", "https://thirddimension.pk/"], "clicks": 5, "impressions": 300, "ctr": 0.016, "position": 14.0},
                {"keys": ["random query", "https://thirddimension.pk/products/other"], "clicks": 1, "impressions": 5, "ctr": 0.2, "position": 3}]})
        if "pagespeedonline" in u:
            assert req.url.params.get("key") == PSI_KEY and req.url.params.get("strategy") == "mobile"
            if s.psi_fail:
                return httpx.Response(429, json={"error": {"message": "Quota exceeded"}})
            slow = "collections/batman" in req.url.params["url"]
            return httpx.Response(200, json={
                "lighthouseResult": {"categories": {"performance": {"score": 0.38 if slow else 0.71}, "seo": {"score": 0.92},
                                                    "accessibility": {"score": 0.8}, "best-practices": {"score": 0.96}},
                                     "audits": {"largest-contentful-paint": {"numericValue": 5400}, "cumulative-layout-shift": {"numericValue": 0.02},
                                                "total-blocking-time": {"numericValue": 300}}},
                "loadingExperience": {"origin_fallback": True, "metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 9999}}}})
        if "urlInspection" in u:
            url = json.loads(req.content)["inspectionUrl"]
            verdict = "NEUTRAL" if "lord-of-the-rings" in url else "PASS"
            return httpx.Response(200, json={"inspectionResult": {"indexStatusResult": {
                "verdict": verdict, "coverageState": "Discovered - currently not indexed" if verdict != "PASS" else "Submitted and indexed",
                "lastCrawlTime": "2026-10-30T10:00:00Z", "googleCanonical": url}}})
        if host == "api.dataforseo.com":
            return s.dataforseo(req)
        if host == "sb.supabase.co":
            table = req.url.path.rsplit("/", 1)[-1]
            if req.method == "GET":
                if table == "seo_rankings":
                    rows = s.rank_history
                    if req.url.params.get("captured_at"):  # "already checked today?" probe
                        since = req.url.params["captured_at"].removeprefix("gte.")
                        rows = [r for r in rows if r["captured_at"] >= since]
                    return httpx.Response(200, json=rows)
                return httpx.Response(200, json=s.drafts)
            if req.method == "PATCH":
                s.patches.append(json.loads(req.content)); return httpx.Response(204)
            rows = json.loads(req.content)
            s.inserts.setdefault(table, []).append({"rows": rows, "on_conflict": req.url.params.get("on_conflict"),
                                                    "prefer": req.headers.get("prefer")})
            return httpx.Response(201, json=[{"id": "run-1", **r} for r in rows])
        if host == "api.telegram.org":
            s.telegram.append(json.loads(req.content)["text"]); return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request {req.method} {host}")

    def dataforseo(s, req):
        s.dfs_auth.add(req.headers.get("authorization"))
        if req.url.path.endswith("/task_post"):
            if s.dfs_post_status != 20000:
                return httpx.Response(401, json={"status_code": s.dfs_post_status, "status_message": "You are not authorized"})
            tasks = []
            for t in json.loads(req.content):
                assert t["location_name"] == "Pakistan" and t["language_code"] == "en" and t["depth"] == 100
                tid = f"t{len(s.dfs_tasks)}"
                s.dfs_tasks[tid] = (t["keyword"], t["device"])
                tasks.append({"id": tid, "status_code": 20100, "status_message": "Task Created."})
            return httpx.Response(200, json={"status_code": 20000, "tasks": tasks})
        tid = req.url.path.rsplit("/", 1)[-1]
        s.dfs_gets[tid] = s.dfs_gets.get(tid, 0) + 1
        if s.dfs_always_queued or s.dfs_gets[tid] == 1:
            return httpx.Response(200, json={"status_code": 20000, "tasks": [{"status_code": 40602, "status_message": "Task In Queue."}]})
        q, d = s.dfs_tasks[tid]
        items = [{"type": "organic", "rank_group": 1, "domain": "www.daraz.pk", "url": "https://www.daraz.pk/x"},
                 {"type": "paid", "rank_group": 1, "domain": "thirddimension.pk", "url": "https://thirddimension.pk/ad"}]
        if q == "batman lamp":
            items.append({"type": "organic", "rank_group": 7 if d == "desktop" else 9, "domain": "www.thirddimension.pk",
                          "url": "https://thirddimension.pk/products/batman-rooftop-signal-lamp"})
        return httpx.Response(200, json={"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"items": items}]}]})

DFS_LOGIN, DFS_PASSWORD = "ops@thirddimension.pk", "dfs-password-secret"
MORNING = dt.datetime(2026, 11, 1, 4, 35, tzinfo=dt.timezone.utc)   # 09:35 PKT
EVENING = dt.datetime(2026, 11, 1, 16, 35, tzinfo=dt.timezone.utc)  # 21:35 PKT

ENV = {"GOOGLE_SA_JSON": "{}", "PSI_API_KEY": PSI_KEY, "SUPABASE_URL": "https://sb.supabase.co",
       "SUPABASE_SERVICE_KEY": "service-secret", "TELEGRAM_BOT_TOKEN": TG_TOKEN, "TELEGRAM_CHAT_ID": "42"}

def go(fake, env=ENV, now=EVENING, sleeps=None):
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return S.run(env=dict(env), client=client, token_fn=lambda _: "tok", today=TODAY,
                 targets_path=os.path.join(ROOT, "seo", "targets.json"), now=now,
                 rank_sleep=(sleeps.append if sleeps is not None else (lambda _: None)))

RANK_ENV = {**ENV, "DATAFORSEO_LOGIN": DFS_LOGIN, "DATAFORSEO_PASSWORD": DFS_PASSWORD}
YESTERDAY = "2026-10-31T04:40:00+00:00"


def test_full_run():
    f = Fake()
    status = go(f)
    rep = f.telegram[0]
    assert status == "ok", status
    assert f.gsc_bodies[0]["startDate"] == "2026-10-23" and f.gsc_bodies[0]["endDate"] == "2026-10-29"
    assert "Home page: 30 clicks (+10), 900 impr, pos 9.0 (was 11.0); mobile speed 71; indexed" in rep, rep
    assert "Custom orders page: NOT LIVE (HTTP 404)" in rep
    assert "- batman lamp: 8.4 (was 10.1), 150 impr, 1 clicks" in rep
    assert "- bat signal lamp: not ranking yet" in rep
    assert "NOT indexed: Discovered - currently not indexed" in rep
    kinds = sorted((r["page_key"], r["kind"]) for r in f.inserts["seo_suggestions"][0]["rows"])
    assert kinds == [("batman-collection", "slow"), ("batman-signal-lamp", "low_ctr"), ("batman-signal-lamp", "striking_distance"),
                     ("custom-orders", "not_live"), ("home", "striking_distance"), ("lotr-collection", "not_indexed")], kinds
    assert all(r["status"] == "draft" for r in f.inserts["seo_suggestions"][0]["rows"])
    pm = f.inserts["seo_page_metrics"][0]
    assert pm["on_conflict"] == "page_key,window_start,window_end" and "merge-duplicates" in pm["prefer"] and len(pm["rows"]) == 12
    q = f.inserts["seo_query_metrics"][0]["rows"]
    assert not any(r["query"] == "random query" for r in q) and any(r["query"] == "batman lamp" and r["tracked"] for r in q)
    ps = f.inserts["seo_pagespeed"][0]["rows"]
    assert len(ps) == 5 and all(r["field_lcp_ms"] is None for r in ps), "origin-fallback field data must be ignored"
    assert f.patches[-1]["status"] == "ok" and f.patches[-1]["site_url"] == "sc-domain:thirddimension.pk"

def test_repeat_run_no_duplicate_drafts():
    f = Fake()
    go(f)
    first = f.inserts["seo_suggestions"][0]["rows"]
    for status, decided_at in [("draft", None), ("rejected", "2020-01-01"), ("done", dt.datetime.now(dt.timezone.utc).isoformat())]:
        previous = [{"page_key": r["page_key"], "kind": r["kind"], "detail": r["detail"], "status": status, "decided_at": decided_at} for r in first]
        f2 = Fake(drafts=previous)
        go(f2)
        assert "seo_suggestions" not in f2.inserts and "New draft suggestions" not in f2.telegram[0], status
    # Applied over 30 days ago and still a problem: suggested again.
    old = [{"page_key": r["page_key"], "kind": r["kind"], "detail": r["detail"], "status": "done", "decided_at": "2020-01-01"} for r in first]
    f3 = Fake(drafts=old)
    go(f3)
    assert len(f3.inserts["seo_suggestions"][0]["rows"]) == len(first)

def test_failures_reported_no_secrets():
    f3 = Fake(psi_fail=True)
    env = {**ENV, "GOOGLE_SA_JSON": ""}
    status = go(f3, env)
    rep = f3.telegram[0]
    assert status == "failed" and "GOOGLE_SA_JSON is not set" in rep and "PageSpeed: HTTP 429" in rep, rep
    blob = rep + json.dumps(f3.patches)
    assert PSI_KEY not in blob and TG_TOKEN not in blob and "service-secret" not in blob

def test_property_detection():
    assert S.pick_site(httpx.Client(transport=httpx.MockTransport(Fake(sites=("https://thirddimension.pk/",)))), "t", "https://thirddimension.pk") == "https://thirddimension.pk/"
    with pytest.raises(S.StepError, match="add its email as a user"):
        S.pick_site(httpx.Client(transport=httpx.MockTransport(Fake(sites=()))), "t", "https://thirddimension.pk")


# ── Rankings (DataForSEO) ──

def test_rankings_morning_run():
    history = [{"query": "batman lamp", "device": "desktop", "position": 10, "captured_at": YESTERDAY},
               {"query": "batman lamp", "device": "mobile", "position": None, "captured_at": YESTERDAY},
               {"query": "bat signal lamp", "device": "mobile", "position": 40, "captured_at": YESTERDAY}]
    f, sleeps = Fake(rank_history=history), []
    status = go(f, RANK_ENV, now=MORNING, sleeps=sleeps)
    rep = f.telegram[0]
    assert status == "ok", rep
    assert len(f.dfs_tasks) == 24 and sleeps, "12 queries x 2 devices, polled until ready"
    assert f.dfs_auth == {"Basic " + base64.b64encode(f"{DFS_LOGIN}:{DFS_PASSWORD}".encode()).decode()}
    assert "Rankings (Google Pakistan, top 100" in rep
    assert "- batman lamp: desktop 7 (up 3), mobile 9 (new in top 100)" in rep, rep
    assert "- bat signal lamp: desktop not in top 100, mobile not in top 100 (dropped out, was 40)" in rep
    rows = f.inserts["seo_rankings"][0]["rows"]
    lamp = next(r for r in rows if r["query"] == "batman lamp" and r["device"] == "desktop")
    assert len(rows) == 24 and lamp["position"] == 7 and lamp["run_id"] == "run-1"
    assert lamp["ranking_url"].endswith("/batman-rooftop-signal-lamp")
    blob = rep + json.dumps(f.patches) + json.dumps(f.inserts)
    assert DFS_PASSWORD not in blob and DFS_LOGIN not in blob


def test_rankings_only_once_a_day_and_only_mornings():
    today_row = [{"query": "batman lamp", "device": "desktop", "position": 7, "captured_at": "2026-11-01T04:20:00+00:00"}]
    for env, now, history in [(ENV, MORNING, []),             # no credentials
                              (RANK_ENV, EVENING, []),        # 21:30 run
                              (RANK_ENV, MORNING, today_row)]:  # already checked today
        f = Fake(rank_history=history)
        assert go(f, env, now=now) == "ok"
        assert not f.dfs_auth and "seo_rankings" not in f.inserts and "Rankings" not in f.telegram[0]


def test_rankings_failure_is_isolated():
    f = Fake(dfs_post_status=40100)
    status = go(f, RANK_ENV, now=MORNING)
    rep = f.telegram[0]
    assert status == "partial" and "Rankings: DataForSEO task_post: 40100 You are not authorized" in rep
    assert "Home page: 30 clicks" in rep and "seo_pagespeed" in f.inserts
    assert DFS_PASSWORD not in rep


def test_rankings_queue_timeout():
    f = Fake(dfs_always_queued=True)
    client = httpx.Client(transport=httpx.MockTransport(f))
    creds = (DFS_LOGIN, DFS_PASSWORD)
    tasks = R.post_tasks(client, creds, ["batman lamp"])
    results, errors = R.collect(client, creds, tasks, wait_seconds=0, sleep=lambda _: None)
    assert results == {} and "still queued" in errors[0]


def test_find_position():
    items = [{"type": "paid", "rank_group": 1, "domain": "thirddimension.pk", "url": "ad"},
             {"type": "organic", "rank_group": 4, "domain": "shop.thirddimension.pk", "url": "u4"},
             {"type": "organic", "rank_group": 2, "domain": "notthirddimension.pk", "url": "x"}]
    assert R.find_position(items) == (4, "u4")
    assert R.find_position([{"type": "organic", "rank_group": 101, "domain": "thirddimension.pk"}]) == (None, None)
    assert R.find_position([]) == (None, None)
