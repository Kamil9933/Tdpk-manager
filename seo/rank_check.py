"""
Daily Google rank check for the queries in seo/targets.json, via the
DataForSEO Google Organic SERP API. Called by seo_report.py on the morning
(09:30 PKT) run only.

Checked against DataForSEO's docs on 3 Nov 2026:
  - Auth: HTTP Basic with the API login and password (DATAFORSEO_LOGIN,
    DATAFORSEO_PASSWORD), sent as a header, never in the URL.
  - Standard queue: POST /v3/serp/google/organic/task_post (up to 100 tasks
    per call), then GET /v3/serp/google/organic/task_get/advanced/{id}.
    Status 20000/20100 = OK, 40601/40602 = still queued. Results are kept
    for 30 days.
  - Price: $0.0006 per 10 results in the standard queue (Live mode is
    $0.002), so depth 100 = $0.006 per query and device. 12 queries x 2
    devices = about $0.14 a day.

Tasks are posted at the start of the run and collected at the end, so the
queue time overlaps with the Search Console and PageSpeed steps.

Position = organic position (rank_group: ads, maps and other blocks don't
count), first result on thirddimension.pk or a subdomain, up to 100.
"""
import base64
import datetime as dt
import time

import httpx

API = "https://api.dataforseo.com/v3/serp/google/organic"
DEPTH = 100
LOCATION = "Pakistan"
LANGUAGE = "en"
DEVICES = ("desktop", "mobile")
OK, CREATED, PENDING = 20000, 20100, (40601, 40602)
WAIT_SECONDS = 600
POLL_SECONDS = 20
PKT = dt.timezone(dt.timedelta(hours=5))


class RankError(Exception):
    pass


def credentials(env):
    login, password = env.get("DATAFORSEO_LOGIN"), env.get("DATAFORSEO_PASSWORD")
    return (login, password) if login and password else None


def _headers(creds):
    token = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def is_morning_run(now_utc):
    """The 09:30 PKT run. GitHub cron often starts late, so anything from
    09:00 to 15:59 PKT counts; the 21:30 run never does."""
    return 9 <= now_utc.astimezone(PKT).hour < 16


def _request(client, what, method, url, creds, **kw):
    """Errors carry the status and DataForSEO's message, never the URL or
    the auth header."""
    try:
        resp = client.request(method, url, headers=_headers(creds), timeout=60, **kw)
    except httpx.HTTPError as e:
        raise RankError(f"{what}: {type(e).__name__}") from None
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or body.get("status_code") != OK:
        raise RankError(f"{what}: {body.get('status_code') or 'HTTP ' + str(resp.status_code)} "
                        f"{(body.get('status_message') or resp.text)[:150]}")
    return body


def post_tasks(client, creds, queries, devices=DEVICES):
    """One standard-queue task per (query, device). Returns {task_id: (query, device)}."""
    wanted = [(q.strip().lower(), d) for q in queries for d in devices]
    body = [{"keyword": q, "location_name": LOCATION, "language_code": LANGUAGE, "device": d,
             "depth": DEPTH, "tag": f"{d}|{q}"[:255]} for q, d in wanted]
    tasks = {}
    for i in range(0, len(body), 100):
        resp = _request(client, "DataForSEO task_post", "POST", f"{API}/task_post", creds, json=body[i:i + 100])
        for t, (q, d) in zip(resp.get("tasks") or [], wanted[i:i + 100]):
            if t.get("status_code") == CREATED and t.get("id"):
                tasks[t["id"]] = (q, d)
            else:
                raise RankError(f"DataForSEO task_post: {t.get('status_code')} {t.get('status_message', '')[:150]}")
    return tasks


def find_position(items, domain="thirddimension.pk"):
    """(organic position, url) of the first result on the domain, else (None, None)."""
    for it in sorted((i for i in items or [] if i.get("type") == "organic"), key=lambda i: i.get("rank_group") or 999):
        host = (it.get("domain") or "").lower().removeprefix("www.")
        if host == domain or host.endswith("." + domain):
            pos = it.get("rank_group")
            return (pos, it.get("url")) if pos and pos <= DEPTH else (None, None)
    return None, None


def collect(client, creds, tasks, wait_seconds=WAIT_SECONDS, poll_seconds=POLL_SECONDS, sleep=time.sleep):
    """Polls each task until done or the wait runs out. Returns
    ({(query, device): {"position", "url"}}, [errors])."""
    results, errors, pending = {}, [], dict(tasks)
    deadline = time.monotonic() + wait_seconds
    while pending:
        for task_id, (q, d) in list(pending.items()):
            try:
                resp = _request(client, "DataForSEO task_get", "GET", f"{API}/task_get/advanced/{task_id}", creds)
            except RankError as e:
                errors.append(f"{q} ({d}): {e}"); del pending[task_id]; continue
            task = (resp.get("tasks") or [{}])[0]
            if task.get("status_code") in PENDING:
                continue
            if task.get("status_code") != OK:
                errors.append(f"{q} ({d}): {task.get('status_code')} {task.get('status_message', '')[:150]}")
            else:
                items = [i for r in task.get("result") or [] for i in r.get("items") or []]
                pos, url = find_position(items)
                results[(q, d)] = {"position": pos, "url": url}
            del pending[task_id]
        if not pending:
            break
        if time.monotonic() >= deadline:
            errors.append(f"{len(pending)} rank check(s) still queued at DataForSEO after {wait_seconds // 60} min; "
                          f"skipped this time (already paid for, results kept 30 days)")
            break
        sleep(poll_seconds)
    return results, errors


def latest_before(rows, day):
    """{(query, device): position} from the newest check before `day` (PKT).
    rows come from seo_rankings ordered newest first."""
    out = {}
    for r in rows:
        captured = dt.datetime.fromisoformat(r["captured_at"].replace("Z", "+00:00")).astimezone(PKT).date()
        if captured < day:
            out.setdefault((r["query"], r["device"]), r.get("position"))
    return out


def _move(cur, prev, seen):
    """seen: there was a previous check for this query/device (its
    position may be None = not in the top 100 then)."""
    if not seen or (prev is None and cur is None):
        return ""
    if prev is None:
        return f" (new in top {DEPTH})"
    if cur is None:
        return f" (dropped out, was {prev})"
    if cur == prev:
        return " (=)"
    return f" (up {prev - cur})" if cur < prev else f" (down {cur - prev})"


def format_section(queries, results, previous, devices=DEVICES):
    lines = ["", f"Rankings (Google {LOCATION}, top {DEPTH}, change since last check):"]
    for q in [q.strip().lower() for q in queries]:
        parts = []
        for d in devices:
            if (q, d) not in results:
                parts.append(f"{d} no result")
                continue
            pos, prev = results[(q, d)]["position"], previous.get((q, d))
            parts.append(f"{d} {pos if pos else 'not in top ' + str(DEPTH)}{_move(pos, prev, (q, d) in previous)}")
        lines.append(f"- {q}: " + ", ".join(parts))
    return lines
