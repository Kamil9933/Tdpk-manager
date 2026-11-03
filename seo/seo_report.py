"""
Twice-daily SEO report for thirddimension.pk -- run by GitHub Actions
(.github/workflows/seo-report.yml), never by the Render app, so it costs no
Render hours and can't slow the dashboard down.

Each run:
  1. Checks each target page in seo/targets.json is live (HTTP 200).
  2. Pulls Search Console data for the last 7 days with final data (Search
     Console lags ~3 days) and the 7 days before that: per-page totals, and
     per (query, page) rows for tracked queries and for every query that
     brings a target page impressions. "Ranking" here is Search Console's
     average position -- the only ranking number Google actually gives you.
  3. Runs PageSpeed Insights (mobile) on each live target page.
  4. Asks the URL Inspection API whether each live page is indexed.
  5. Turns problems into DRAFT suggestions in Supabase (seo_suggestions).
  5b. Morning run only: checks each tracked query's real Google position in
     Pakistan (desktop + mobile) via DataForSEO -- see seo/rank_check.py.
     Nothing is ever changed on Shopify: you approve and apply them yourself.
  6. Stores everything in Supabase (seo_schema.sql) and sends a Telegram
     report. Every section fails on its own -- a PageSpeed outage still
     gets you the Search Console part, with the error listed.

Environment (GitHub Actions secrets):
  GOOGLE_SA_JSON        required: service-account key JSON. The service
                        account's email must be added as a user (Restricted
                        is enough) on the Search Console property.
  GSC_SITE_URL          optional: "sc-domain:thirddimension.pk" (Domain
                        property) or "https://thirddimension.pk/" (URL-prefix).
                        If unset, the script finds whichever the account can see.
  PSI_API_KEY           needed for PageSpeed: Google API key with the PageSpeed
                        Insights API enabled. Keyless calls share a global quota
                        that was already exhausted when this was tested.
  SUPABASE_URL, SUPABASE_SERVICE_KEY      optional: storage; skipped if unset.
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID    optional: report; printed if unset.
  DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD   optional: daily rank check; skipped if unset.

Error text never includes request URLs, since those can carry API keys.
"""
import os
import sys
import json
import time
import datetime as dt
from urllib.parse import quote, urlparse

import httpx

import rank_check

WEBMASTERS_API = "https://www.googleapis.com/webmasters/v3"
INSPECTION_API = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
PSI_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
GSC_LAG_DAYS = 3
WINDOW_DAYS = 7
# Rough organic CTR by average position; "low CTR" means under half of this.
EXPECTED_CTR = [(1.5, 0.25), (2.5, 0.13), (3.5, 0.09), (5.5, 0.06), (10.5, 0.03)]
STRIKING_MIN_IMPRESSIONS = 20
LOW_CTR_MIN_IMPRESSIONS = 100
SLOW_SCORE = 50
TELEGRAM_LIMIT = 4000


class StepError(Exception):
    pass


def google_token(sa_json):
    """OAuth access token for the service account. Kept separate so tests
    can swap it out without real credentials."""
    from google.oauth2 import service_account
    import google.auth.transport.requests
    creds = service_account.Credentials.from_service_account_info(json.loads(sa_json), scopes=SCOPES)
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def _norm(url):
    return (url or "").strip().lower().rstrip("/")


def _http_error(what, resp):
    return StepError(f"{what}: HTTP {resp.status_code} {resp.text[:200]}")


def _call(client, what, method, url, **kw):
    """One HTTP call, with errors that never contain the request URL."""
    try:
        resp = client.request(method, url, **kw)
    except httpx.HTTPError as e:
        raise StepError(f"{what}: {type(e).__name__}") from None
    if resp.status_code >= 400:
        raise _http_error(what, resp)
    return resp


# ── Search Console ──────────────────────────────────────────────────────────

def pick_site(client, token, site, configured=None):
    if configured:
        return configured
    data = _call(client, "Search Console site list", "GET", f"{WEBMASTERS_API}/sites",
                 headers={"Authorization": f"Bearer {token}"}).json()
    visible = {e["siteUrl"]: e.get("permissionLevel") for e in data.get("siteEntry", [])
               if e.get("permissionLevel") != "siteUnverifiedUser"}
    host = urlparse(site).hostname
    for candidate in (f"sc-domain:{host}", f"https://{host}/", f"https://www.{host}/", f"http://{host}/"):
        if candidate in visible:
            return candidate
    raise StepError(f"the service account can't see a Search Console property for {host} -- "
                    f"add its email as a user on the property (visible: {sorted(visible) or 'none'})")


def gsc_rows(client, token, site_url, start, end, dimensions):
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
            "dimensions": dimensions, "rowLimit": 5000, "dataState": "final"}
    resp = _call(client, f"Search Console {'+'.join(dimensions)} query", "POST",
                 f"{WEBMASTERS_API}/sites/{quote(site_url, safe='')}/searchAnalytics/query",
                 headers={"Authorization": f"Bearer {token}"}, json=body)
    return resp.json().get("rows", [])


def windows(today):
    end = today - dt.timedelta(days=GSC_LAG_DAYS)
    cur = (end - dt.timedelta(days=WINDOW_DAYS - 1), end)
    prev = (cur[0] - dt.timedelta(days=WINDOW_DAYS), cur[0] - dt.timedelta(days=1))
    return cur, prev


def _metrics(row):
    return {"clicks": int(row.get("clicks", 0)), "impressions": int(row.get("impressions", 0)),
            "ctr": round(float(row.get("ctr", 0)), 4),
            "position": round(float(row["position"]), 1) if row.get("position") is not None else None}


def collect_search(client, token, site_url, targets, today):
    pages = targets["pages"]
    tracked = {q.strip().lower() for q in targets.get("queries", [])}
    by_url = {_norm(p["url"]): p for p in pages}
    out = {"windows": {}, "pages": {}, "queries": []}
    for label, (start, end) in zip(("current", "previous"), windows(today)):
        out["windows"][label] = (start, end)
        page_rows = {_norm(r["keys"][0]): r for r in gsc_rows(client, token, site_url, start, end, ["page"])}
        for p in pages:
            row = page_rows.get(_norm(p["url"]))
            m = _metrics(row) if row else {"clicks": 0, "impressions": 0, "ctr": 0, "position": None}
            out["pages"].setdefault(p["key"], {})[label] = m
        for r in gsc_rows(client, token, site_url, start, end, ["query", "page"]):
            query, page = r["keys"][0].strip().lower(), r["keys"][1]
            target = by_url.get(_norm(page))
            if target or query in tracked:
                out["queries"].append({"window": label, "query": query, "page_url": page,
                                       "page_key": target["key"] if target else None,
                                       "tracked": query in tracked, **_metrics(r)})
    return out


def inspect_url(client, token, site_url, url):
    resp = _call(client, "URL inspection", "POST", INSPECTION_API,
                 headers={"Authorization": f"Bearer {token}"},
                 json={"inspectionUrl": url, "siteUrl": site_url})
    idx = (resp.json().get("inspectionResult") or {}).get("indexStatusResult") or {}
    return {"verdict": idx.get("verdict"), "coverage_state": idx.get("coverageState"),
            "last_crawl_time": idx.get("lastCrawlTime"), "google_canonical": idx.get("googleCanonical")}


# ── PageSpeed + liveness ────────────────────────────────────────────────────

def is_live(client, url):
    try:
        resp = client.get(url, follow_redirects=True, headers={"User-Agent": "TDPK-SEO-report"})
    except httpx.HTTPError as e:
        return False, type(e).__name__
    return resp.status_code == 200, f"HTTP {resp.status_code}"


def pagespeed(client, url, api_key=None):
    params = [("url", url), ("strategy", "mobile")] + [("category", c) for c in
              ("performance", "seo", "accessibility", "best-practices")]
    if api_key:
        params.append(("key", api_key))
    data = _call(client, "PageSpeed", "GET", PSI_API, params=params, timeout=120).json()
    lh = data.get("lighthouseResult") or {}
    cats, audits = lh.get("categories") or {}, lh.get("audits") or {}

    def score(name):
        s = (cats.get(name) or {}).get("score")
        return round(s * 100) if s is not None else None

    def audit(name):
        return (audits.get(name) or {}).get("numericValue")

    field = data.get("loadingExperience") or {}
    fm = field.get("metrics") or {} if not field.get("origin_fallback") else {}

    def pct(name):
        return (fm.get(name) or {}).get("percentile")

    cls_field = pct("CUMULATIVE_LAYOUT_SHIFT_SCORE")
    return {"performance": score("performance"), "seo": score("seo"),
            "accessibility": score("accessibility"), "best_practices": score("best-practices"),
            "lcp_ms": audit("largest-contentful-paint"), "cls": audit("cumulative-layout-shift"),
            "tbt_ms": audit("total-blocking-time"),
            "field_lcp_ms": pct("LARGEST_CONTENTFUL_PAINT_MS"), "field_inp_ms": pct("INTERACTION_TO_NEXT_PAINT"),
            "field_cls": cls_field / 100 if cls_field is not None else None}


# ── Suggestions (drafts only) ───────────────────────────────────────────────

def _expected_ctr(position):
    for limit, ctr in EXPECTED_CTR:
        if position <= limit:
            return ctr
    return None


def build_suggestions(targets, live, search, speed, index):
    out = []
    for p in targets["pages"]:
        key, name = p["key"], p["name"]
        if not live.get(key, (True, ""))[0]:
            out.append({"page_key": key, "kind": "not_live", "detail": {"status": live[key][1]},
                        "suggestion": f"{name} isn't live ({live[key][1]}). Publish it in Shopify once the draft is ready."})
            continue
        ix = index.get(key) or {}
        if ix.get("verdict") and ix["verdict"] != "PASS":
            out.append({"page_key": key, "kind": "not_indexed", "detail": ix,
                        "suggestion": f"{name} isn't indexed ({ix.get('coverage_state') or ix['verdict']}). "
                                      f"Check it in Search Console and request indexing."})
        cur = ((search or {}).get("pages", {}).get(key) or {}).get("current")
        if cur and cur["position"] and cur["impressions"] >= LOW_CTR_MIN_IMPRESSIONS:
            expected = _expected_ctr(cur["position"])
            if expected and cur["ctr"] < expected / 2:
                out.append({"page_key": key, "kind": "low_ctr", "detail": cur,
                            "suggestion": f"{name}: {cur['ctr']:.1%} CTR at position {cur['position']} "
                                          f"({cur['impressions']} impressions) -- about {expected:.0%} is typical. "
                                          f"Draft a sharper SEO title/meta description."})
        striking = sorted((q for q in (search or {}).get("queries", [])
                           if q["window"] == "current" and q["page_key"] == key and q["position"]
                           and 4 <= q["position"] <= 20 and q["impressions"] >= STRIKING_MIN_IMPRESSIONS),
                          key=lambda q: -q["impressions"])[:3]
        for q in striking:
            out.append({"page_key": key, "kind": "striking_distance",
                        "detail": {"query": q["query"], "position": q["position"], "impressions": q["impressions"]},
                        "suggestion": f"{name} ranks {q['position']} for \"{q['query']}\" ({q['impressions']} impressions). "
                                      f"Work the phrase into the title, H1 and description to push it onto page 1."})
        sp = speed.get(key) or {}
        if sp.get("performance") is not None and sp["performance"] < SLOW_SCORE:
            out.append({"page_key": key, "kind": "slow", "detail": {"performance": sp["performance"], "lcp_ms": sp.get("lcp_ms")},
                        "suggestion": f"{name} scores {sp['performance']}/100 on mobile speed "
                                      f"(LCP {round((sp.get('lcp_ms') or 0) / 1000, 1)}s). Check image sizes and apps on this page."})
    return out


def _suggestion_id(s):
    return (s["page_key"], s["kind"], (s.get("detail") or {}).get("query", ""))


# ── Supabase ────────────────────────────────────────────────────────────────

class Store:
    def __init__(self, client, url, key):
        self.client, self.base = client, f"{url.rstrip('/')}/rest/v1"
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def insert(self, table, rows, on_conflict=None):
        if not rows:
            return []
        headers = {**self.headers, "Prefer": "return=representation"
                   + (",resolution=merge-duplicates" if on_conflict else "")}
        params = {"on_conflict": on_conflict} if on_conflict else None
        return _call(self.client, f"Supabase insert {table}", "POST", f"{self.base}/{table}",
                     headers=headers, params=params, json=rows).json()

    def select(self, table, params):
        return _call(self.client, f"Supabase read {table}", "GET", f"{self.base}/{table}",
                     headers=self.headers, params=params).json()

    def update(self, table, row_id, data):
        _call(self.client, f"Supabase update {table}", "PATCH", f"{self.base}/{table}",
              headers=self.headers, params={"id": f"eq.{row_id}"}, json=data)


# ── Report ──────────────────────────────────────────────────────────────────

def _change(cur, prev, fmt="{:+,}"):
    return f" ({fmt.format(cur - prev)})" if prev is not None and cur != prev else ""


def format_report(targets, site_url, live, search, speed, index, new_suggestions, errors, rankings=None):
    lines = ["TDPK SEO report -- thirddimension.pk"]
    if search:
        (cs, ce), _ = search["windows"]["current"], search["windows"]["previous"]
        lines.append(f"Search Console {cs:%d %b}-{ce:%d %b} vs the 7 days before ({site_url})")
    lines.append("")
    for p in targets["pages"]:
        key = p["key"]
        if not live.get(key, (True, ""))[0]:
            lines.append(f"{p['name']}: NOT LIVE ({live[key][1]})")
            continue
        parts = []
        pm = (search or {}).get("pages", {}).get(key)
        if pm:
            c, pv = pm["current"], pm.get("previous") or {}
            if not c["impressions"]:
                parts.append("no search impressions" + (f" (had {pv['impressions']:,})" if pv.get("impressions") else ""))
            else:
                was = f" (was {pv['position']})" if pv.get("position") and pv["position"] != c["position"] else ""
                parts.append(f"{c['clicks']} clicks{_change(c['clicks'], pv.get('clicks'))}, "
                             f"{c['impressions']:,} impr{_change(c['impressions'], pv.get('impressions'))}, "
                             f"pos {c['position']}{was}")
        sp = speed.get(key) or {}
        if sp.get("performance") is not None:
            parts.append(f"mobile speed {sp['performance']}")
        ix = index.get(key) or {}
        if ix.get("verdict"):
            parts.append("indexed" if ix["verdict"] == "PASS" else f"NOT indexed: {ix.get('coverage_state') or ix['verdict']}")
        lines.append(f"{p['name']}: " + ("; ".join(parts) or "no data"))

    if search:
        tracked = {}
        for q in search["queries"]:
            if q["tracked"]:
                t = tracked.setdefault(q["query"], {})
                best = t.get(q["window"])
                if not best or q["impressions"] > best["impressions"]:
                    t[q["window"]] = q
        lines.append("")
        lines.append("Tracked searches (avg position, best page):")
        for query in [q.strip().lower() for q in targets.get("queries", [])]:
            t = tracked.get(query) or {}
            c, pv = t.get("current"), t.get("previous")
            if not c:
                lines.append(f"- {query}: not ranking yet")
                continue
            was = f" (was {pv['position']})" if pv and pv["position"] != c["position"] else ""
            lines.append(f"- {query}: {c['position']}{was}, {c['impressions']} impr, {c['clicks']} clicks")

    if rankings:
        lines += rankings
    if new_suggestions:
        lines += ["", f"New draft suggestions ({len(new_suggestions)}) -- nothing changed on the site:"]
        lines += [f"- {s['suggestion']}" for s in new_suggestions]
    if errors:
        lines += ["", "Problems this run:"] + [f"- {e}" for e in errors]
    text = "\n".join(lines)
    return text if len(text) <= TELEGRAM_LIMIT else text[:TELEGRAM_LIMIT - 20] + "\n...(truncated)"


def send_telegram(client, token, chat_id, text):
    try:
        resp = client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat_id, "text": text})
    except httpx.HTTPError as e:
        raise StepError(f"Telegram: {type(e).__name__}") from None
    try:
        ok = resp.json().get("ok")
    except ValueError:
        ok = False
    if resp.status_code >= 400 or not ok:
        raise StepError(f"Telegram: HTTP {resp.status_code}")


# ── Main ────────────────────────────────────────────────────────────────────

def run(env=None, client=None, token_fn=google_token, today=None, targets_path=None, now=None, rank_sleep=time.sleep):
    env = env if env is not None else os.environ
    targets_path = targets_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets.json")
    targets = json.load(open(targets_path, encoding="utf-8"))
    today = today or dt.date.today()
    client = client or httpx.Client(timeout=60)
    errors, live, speed, index = [], {}, {}, {}
    search, site_url, token = None, None, None

    store = None
    if env.get("SUPABASE_URL") and env.get("SUPABASE_SERVICE_KEY"):
        store = Store(client, env["SUPABASE_URL"], env["SUPABASE_SERVICE_KEY"])
    run_id = None
    if store:
        try:
            run_id = store.insert("seo_runs", [{"status": "running"}])[0]["id"]
        except StepError as e:
            errors.append(str(e)); store = None

    # Rankings: post the DataForSEO tasks now and collect them at the end,
    # so their queue time overlaps with the steps below. Morning run only,
    # and never twice in one day (each check costs money).
    now = now or dt.datetime.now(dt.timezone.utc)
    rank_creds, rank_tasks, rankings = rank_check.credentials(env), None, None
    if rank_creds and rank_check.is_morning_run(now):
        try:
            day_start = dt.datetime.combine(now.astimezone(rank_check.PKT).date(), dt.time(), rank_check.PKT)
            if store and store.select("seo_rankings", {"select": "id", "captured_at": f"gte.{day_start.isoformat()}", "limit": "1"}):
                print("Rankings already checked today; skipping.")
            else:
                rank_tasks = rank_check.post_tasks(client, rank_creds, targets.get("queries", []))
        except (StepError, rank_check.RankError) as e:
            errors.append(f"Rankings: {e}")

    for p in targets["pages"]:
        live[p["key"]] = is_live(client, p["url"])

    try:
        if not env.get("GOOGLE_SA_JSON"):
            raise StepError("GOOGLE_SA_JSON is not set")
        try:
            token = token_fn(env["GOOGLE_SA_JSON"])
        except StepError:
            raise
        except Exception as e:
            raise StepError(f"Google service-account login failed: {type(e).__name__}") from None
        site_url = pick_site(client, token, targets["site"], env.get("GSC_SITE_URL"))
        search = collect_search(client, token, site_url, targets, today)
    except StepError as e:
        errors.append(f"Search Console: {e}")

    for p in targets["pages"]:
        if not live[p["key"]][0]:
            continue
        try:
            speed[p["key"]] = pagespeed(client, p["url"], env.get("PSI_API_KEY"))
        except StepError as e:
            speed[p["key"]] = {"error": str(e)}
            errors.append(f"{p['name']}: {e}")
        if token and site_url:
            try:
                index[p["key"]] = inspect_url(client, token, site_url, p["url"])
            except StepError as e:
                index[p["key"]] = {"error": str(e)}
                errors.append(f"{p['name']}: {e}")

    suggestions = build_suggestions(targets, live, search, speed, index)
    new_suggestions = suggestions
    if store:
        try:
            if search:
                rows = []
                for key, by_window in search["pages"].items():
                    url = next(p["url"] for p in targets["pages"] if p["key"] == key)
                    for label, m in by_window.items():
                        start, end = search["windows"][label]
                        rows.append({"page_key": key, "url": url, "window_start": start.isoformat(),
                                     "window_end": end.isoformat(), **m, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
                store.insert("seo_page_metrics", rows, on_conflict="page_key,window_start,window_end")
                qrows = {}
                for q in search["queries"]:
                    start, end = search["windows"][q["window"]]
                    k = (q["query"], q["page_url"], start, end)
                    qrows[k] = {"query": q["query"], "page_url": q["page_url"], "window_start": start.isoformat(),
                                "window_end": end.isoformat(), "tracked": q["tracked"], "clicks": q["clicks"],
                                "impressions": q["impressions"], "ctr": q["ctr"], "position": q["position"],
                                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
                store.insert("seo_query_metrics", list(qrows.values()), on_conflict="query,page_url,window_start,window_end")
            url_of = {p["key"]: p["url"] for p in targets["pages"]}
            store.insert("seo_pagespeed", [{"run_id": run_id, "page_key": k, "url": url_of[k], "strategy": "mobile", **v}
                                           for k, v in speed.items()])
            store.insert("seo_index_status", [{"run_id": run_id, "page_key": k, "url": url_of[k], **v}
                                              for k, v in index.items()])
            # Don't re-suggest: anything still in the queue, anything you
            # rejected, or anything applied in the last 30 days (Search
            # Console lags, so the old numbers would re-trigger it at once).
            recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
            previous = store.select("seo_suggestions", {"select": "page_key,kind,detail,status,decided_at"})
            seen = {_suggestion_id(s) for s in previous
                    if s.get("status") in ("draft", "rejected", "approved")
                    or (s.get("status") == "done" and (s.get("decided_at") or "") >= recent)}
            new_suggestions = [s for s in suggestions if _suggestion_id(s) not in seen]
            store.insert("seo_suggestions", [{**s, "status": "draft"} for s in new_suggestions])
        except StepError as e:
            errors.append(str(e))

    if rank_tasks:
        results, rank_errors = rank_check.collect(client, rank_creds, rank_tasks, sleep=rank_sleep)
        errors += [f"Rankings: {e}" for e in rank_errors]
        previous = {}
        if store:
            try:
                history = store.select("seo_rankings", {"select": "query,device,position,captured_at",
                                                        "order": "captured_at.desc", "limit": "1000"})
                previous = rank_check.latest_before(history, now.astimezone(rank_check.PKT).date())
            except StepError as e:
                errors.append(f"Rankings history: {e}")
            try:
                store.insert("seo_rankings", [{"run_id": run_id, "captured_at": now.isoformat(), "query": q, "device": d,
                                               "position": r["position"], "ranking_url": r["url"]}
                                              for (q, d), r in results.items()])
            except StepError as e:
                errors.append(str(e))
        rankings = rank_check.format_section(targets.get("queries", []), results, previous)

    report = format_report(targets, site_url, live, search, speed, index, new_suggestions, errors, rankings)
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        try:
            send_telegram(client, env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], report)
        except StepError as e:
            errors.append(str(e))
    print(report)

    status = "failed" if not search else ("partial" if errors else "ok")
    if store and run_id:
        try:
            store.update("seo_runs", run_id, {"status": status, "site_url": site_url, "errors": errors,
                                              "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        except StepError as e:
            print(f"Could not close the run record: {e}")
    return status


if __name__ == "__main__":
    sys.exit(1 if run() == "failed" else 0)
