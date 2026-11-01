"""SEO report script end-to-end against fake Google, PageSpeed, Supabase and Telegram."""
import os, json, datetime as dt
from conftest import ROOT
import httpx
import pytest
import seo_report as S

PSI_KEY = "AIzaPSIKEYsecret_1234567890abcdefgh"
TG_TOKEN = "123456789:AAHtelegramsecretsecretsecret"
TODAY = dt.date(2026, 11, 1)  # current window 23-29 Oct, previous 16-22 Oct

class Fake:
    def __init__(s, psi_fail=False, drafts=None, sites=("sc-domain:thirddimension.pk",)):
        s.psi_fail, s.drafts, s.sites = psi_fail, drafts or [], sites
        s.inserts, s.patches, s.telegram, s.gsc_bodies = {}, [], [], []
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
        if host == "sb.supabase.co":
            table = req.url.path.rsplit("/", 1)[-1]
            if req.method == "GET":
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

ENV = {"GOOGLE_SA_JSON": "{}", "PSI_API_KEY": PSI_KEY, "SUPABASE_URL": "https://sb.supabase.co",
       "SUPABASE_SERVICE_KEY": "service-secret", "TELEGRAM_BOT_TOKEN": TG_TOKEN, "TELEGRAM_CHAT_ID": "42"}

def go(fake, env=ENV):
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return S.run(env=dict(env), client=client, token_fn=lambda _: "tok", today=TODAY,
                 targets_path=os.path.join(ROOT, "seo", "targets.json"))


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
