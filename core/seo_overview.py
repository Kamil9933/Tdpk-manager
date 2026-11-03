"""
SEO Rankings tab data (GET /api/seo/overview). Read-only: turns the rows that
seo/seo_report.py stores in Supabase into the cards, chart series and tables
the dashboard draws. Pure functions, so tests need no database.

What the numbers are:
  - Search Console rows (seo_query_metrics, seo_page_metrics) are 7-day
    windows, one per daily run, dated by window_end (Search Console lags
    ~3 days). A keyword's "position" on a date is Google's average position
    over that window, for the page that got the most impressions.
  - seo_rankings (DataForSEO, if the table exists) is the exact position on
    the day it was checked, per device.
  - Cards compare the latest window with the window 7 days earlier.
  - Lower position numbers are better everywhere.
"""
import datetime as dt

PKT = dt.timezone(dt.timedelta(hours=5))
PERIODS = {"7": 7, "30": 30, "90": 90, "all": None}
BUCKETS = [("1-3", 1, 3), ("4-10", 4, 10), ("11-20", 11, 20), ("21-50", 21, 50), ("50+", 51, 10_000)]
SPARK_DAYS = 30


def _d(s):
    return dt.date.fromisoformat(str(s)[:10]) if s else None


def _pkt_date(ts):
    return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(PKT).date()


def _at_or_before(history, day, max_gap=7):
    """Newest point dated on/before `day`, if within max_gap days of it."""
    best = None
    for p in history:
        d = _d(p["date"])
        if d <= day and (best is None or d > _d(best["date"])):
            best = p
    return best if best and (day - _d(best["date"])).days <= max_gap else None


def query_histories(rows):
    """{query: [point, ...]} oldest first; one point per window_end, using
    the page with the most impressions that window."""
    best = {}
    for r in rows:
        q = (r.get("query") or "").strip().lower()
        k = (q, str(r["window_end"])[:10])
        cur = best.get(k)
        if cur is None or (r.get("impressions") or 0) > (cur.get("impressions") or 0):
            best[k] = r
    out = {}
    for (q, day), r in sorted(best.items(), key=lambda kv: kv[0][1]):
        out.setdefault(q, []).append({
            "date": day, "position": float(r["position"]) if r.get("position") is not None else None,
            "impressions": r.get("impressions") or 0, "clicks": r.get("clicks") or 0,
            "ctr": float(r.get("ctr") or 0), "page_url": r.get("page_url"), "tracked": bool(r.get("tracked")),
        })
    return out


def _bucket_counts(positions):
    counts = {name: 0 for name, _, _ in BUCKETS}
    for p in positions:
        if p is None:
            continue
        for name, lo, hi in BUCKETS:
            if lo <= round(p) <= hi:
                counts[name] += 1
                break
    return counts


def _card(value, previous, lower_is_better=False):
    better = None
    if value is not None and previous is not None and value != previous:
        better = (value < previous) if lower_is_better else (value > previous)
    return {"value": value, "previous": previous, "better": better}


def _keyword_counts(tracked, hist, day):
    pos = []
    for q in tracked:
        p = next((x for x in hist.get(q, []) if x["date"] == day.isoformat()), None) if day else None
        pos.append(p["position"] if p else None)
    return {
        "top3": sum(1 for p in pos if p is not None and p <= 3),
        "top10": sum(1 for p in pos if p is not None and p <= 10),
        "top20": sum(1 for p in pos if p is not None and p <= 20),
        "not_ranking": sum(1 for p in pos if p is None),
    }


def _page_totals(page_rows, keys, day):
    rows = [r for r in page_rows if r["page_key"] in keys and day and str(r["window_end"])[:10] == day.isoformat()]
    if not rows:
        return None
    clicks = sum(r.get("clicks") or 0 for r in rows)
    impr = sum(r.get("impressions") or 0 for r in rows)
    weighted = [(float(r["position"]), r.get("impressions") or 0) for r in rows if r.get("position") is not None]
    w = sum(i for _, i in weighted)
    avg = round(sum(p * i for p, i in weighted) / w, 1) if w else None
    return {"clicks": clicks, "impressions": impr, "position": avg}


def _index_label(row):
    if not row:
        return "Unknown to Google"
    cov = (row.get("coverage_state") or "")
    if row.get("verdict") == "PASS":
        return "Indexed"
    if "unknown to google" in cov.lower():
        return "Unknown to Google"
    return "Not indexed"


def build_overview(targets, last_run, query_rows, page_rows, psi_rows, index_rows, ranking_rows, days="30", today=None):
    today = today or dt.datetime.now(PKT).date()
    span = PERIODS.get(str(days), 30)
    since = today - dt.timedelta(days=span) if span else None
    tracked = [q.strip().lower() for q in targets.get("queries", [])]
    pages = targets.get("pages", [])
    page_keys = {p["key"] for p in pages}
    hist = query_histories(query_rows or [])

    sc_dates = sorted({str(r["window_end"])[:10] for r in (query_rows or [])} |
                      {str(r["window_end"])[:10] for r in (page_rows or [])})
    latest = _d(sc_dates[-1]) if sc_dates else None
    prev = None
    if latest:
        older = [_d(d) for d in sc_dates if _d(d) <= latest - dt.timedelta(days=7)]
        prev = max(older) if older and (latest - dt.timedelta(days=7) - max(older)).days <= 7 else None

    # ── Cards ──
    cur_tot, prev_tot = _page_totals(page_rows or [], page_keys, latest), _page_totals(page_rows or [], page_keys, prev)
    kc_cur, kc_prev = _keyword_counts(tracked, hist, latest), (_keyword_counts(tracked, hist, prev) if prev else {})
    cards = {
        "clicks": _card((cur_tot or {}).get("clicks"), (prev_tot or {}).get("clicks")),
        "impressions": _card((cur_tot or {}).get("impressions"), (prev_tot or {}).get("impressions")),
        "avg_position": _card((cur_tot or {}).get("position"), (prev_tot or {}).get("position"), lower_is_better=True),
        "top3": _card(kc_cur["top3"], kc_prev.get("top3")),
        "top10": _card(kc_cur["top10"], kc_prev.get("top10")),
        "top20": _card(kc_cur["top20"], kc_prev.get("top20")),
        "not_ranking": _card(kc_cur["not_ranking"], kc_prev.get("not_ranking"), lower_is_better=True),
    }

    # ── Exact rank-tracker positions ──
    tracker = None
    if ranking_rows is not None:
        tracker = {}
        for r in sorted(ranking_rows, key=lambda r: str(r["captured_at"])):
            q = (r.get("query") or "").strip().lower()
            tracker.setdefault(q, {}).setdefault(r["device"], {})[_pkt_date(r["captured_at"]).isoformat()] = {
                "position": r.get("position"), "url": r.get("ranking_url")}

    # ── Chart axis: every date with any data in the period ──
    in_period = (lambda day: since is None or _d(day) >= since)
    labels = sorted({p["date"] for q in hist for p in hist[q] if in_period(p["date"])} |
                    {day for q in (tracker or {}) for dev in tracker[q] for day in tracker[q][dev] if in_period(day)})
    chart = {"labels": labels, "search_console": {}, "tracker": {}}
    for q in sorted(set(tracked) | {q for q in hist if any(p["tracked"] for p in hist[q])}):
        by_day = {p["date"]: p for p in hist.get(q, [])}
        chart["search_console"][q] = [
            {"position": by_day[d]["position"], "impressions": by_day[d]["impressions"], "clicks": by_day[d]["clicks"]}
            if d in by_day and by_day[d]["position"] is not None else None for d in labels]
        for dev, days_ in (tracker or {}).get(q, {}).items():
            chart["tracker"].setdefault(q, {})[dev] = [
                {"position": days_[d]["position"]} if d in days_ and days_[d]["position"] is not None else None for d in labels]

    # ── Keywords table ──
    spark_days = [(latest - dt.timedelta(days=i)).isoformat() for i in range(SPARK_DAYS - 1, -1, -1)] if latest else []
    keywords = []
    for q in tracked:
        h = hist.get(q, [])
        cur = next((p for p in h if latest and p["date"] == latest.isoformat()), None)
        before = [p for p in h if cur and p["date"] < cur["date"] and p["position"] is not None]
        prev_p = before[-1] if before else None
        ranked = [p["position"] for p in h if p["position"] is not None]
        by_day = {p["date"]: p["position"] for p in h}
        exact = {dev: days_[max(days_)] for dev, days_ in (tracker or {}).get(q, {}).items()} if tracker else {}
        keywords.append({
            "query": q,
            "position": cur["position"] if cur else None,
            "change": round(prev_p["position"] - cur["position"], 1) if cur and prev_p and cur["position"] is not None else None,
            "best": min(ranked) if ranked else None,
            "impressions": cur["impressions"] if cur else None,
            "clicks": cur["clicks"] if cur else None,
            "ctr": cur["ctr"] if cur else None,
            "page_url": cur["page_url"] if cur else None,
            "last_seen": h[-1]["date"] if h else None,
            "almost_page_1": bool(cur and cur["position"] is not None and 11 <= round(cur["position"]) <= 20),
            "spark": [by_day.get(d) for d in spark_days],
            "exact": {dev: v["position"] for dev, v in exact.items()},
            "status": "ranking" if cur else ("not_showing_now" if h else "not_showing_yet"),
        })

    # ── Movers (all queries, latest window vs the one 7 days earlier) ──
    moves = []
    if latest and prev:
        for q, h in hist.items():
            a = next((p for p in h if p["date"] == latest.isoformat()), None)
            b = _at_or_before(h, prev, max_gap=0)
            if a and b and a["position"] is not None and b["position"] is not None and a["position"] != b["position"]:
                moves.append({"query": q, "position": a["position"], "previous": b["position"],
                              "change": round(b["position"] - a["position"], 1), "tracked": q in tracked})
    movers = {"up": sorted([m for m in moves if m["change"] > 0], key=lambda m: -m["change"])[:5],
              "down": sorted([m for m in moves if m["change"] < 0], key=lambda m: m["change"])[:5]}

    # ── New (untracked) keywords first seen in the last 7 days of data ──
    new_keywords = []
    if latest:
        for q, h in hist.items():
            if q in tracked or not h:
                continue
            if _d(h[0]["date"]) > latest - dt.timedelta(days=7) and h[-1]["date"] == latest.isoformat():
                new_keywords.append({"query": q, "position": h[-1]["position"], "impressions": h[-1]["impressions"],
                                     "page_url": h[-1]["page_url"], "first_seen": h[0]["date"]})
        new_keywords.sort(key=lambda k: -(k["impressions"] or 0))

    # ── Clicks/impressions (7-day totals per window) ──
    traffic = []
    for day in sorted({str(r["window_end"])[:10] for r in (page_rows or [])}):
        if in_period(day):
            t = _page_totals(page_rows, page_keys, _d(day))
            traffic.append({"date": day, "clicks": t["clicks"], "impressions": t["impressions"]})

    # ── Ranking distribution over time (tracked keywords, Search Console) ──
    distribution = []
    for day in labels:
        pos = [next((p["position"] for p in hist.get(q, []) if p["date"] == day), None) for q in tracked]
        if any(p is not None for p in pos):
            distribution.append({"date": day, **_bucket_counts(pos)})

    # ── Pages ──
    page_out, speed = [], {}
    for p in pages:
        k = p["key"]
        ps = sorted([r for r in (psi_rows or []) if r["page_key"] == k and (r.get("strategy") or "mobile") == "mobile"],
                    key=lambda r: str(r["captured_at"]))
        speed[k] = [{"date": _pkt_date(r["captured_at"]).isoformat(), "performance": r.get("performance"),
                     "lcp_ms": r.get("lcp_ms")} for r in ps if r.get("performance") is not None and in_period(_pkt_date(r["captured_at"]).isoformat())]
        last_speed = next((r for r in reversed(ps) if r.get("performance") is not None), None)
        ix = sorted([r for r in (index_rows or []) if r["page_key"] == k and (r.get("verdict") or r.get("coverage_state"))],
                    key=lambda r: str(r["captured_at"]))
        label = _index_label(ix[-1] if ix else None)
        changed = None
        for r in ix:
            if _index_label(r) == label:
                changed = changed or r["captured_at"]
            else:
                changed = None
        m = next((r for r in (page_rows or []) if r["page_key"] == k and latest and str(r["window_end"])[:10] == latest.isoformat()), None)
        page_out.append({
            "key": k, "name": p["name"], "url": p["url"],
            "index": label, "coverage": ix[-1].get("coverage_state") if ix else None, "index_since": changed,
            "speed": last_speed.get("performance") if last_speed else None,
            "lcp_ms": last_speed.get("lcp_ms") if last_speed else None,
            "clicks": m.get("clicks") if m else None, "impressions": m.get("impressions") if m else None,
            "position": float(m["position"]) if m and m.get("position") is not None else None,
        })

    return {
        "days": str(days) if str(days) in PERIODS else "30",
        "last_run": last_run,
        "windows": {"current": latest.isoformat() if latest else None, "previous": prev.isoformat() if prev else None},
        "has_search_data": bool(hist),
        "rankings_available": ranking_rows is not None,
        "cards": cards, "chart": chart, "keywords": keywords,
        "default_keywords": [k["query"] for k in sorted(
            [k for k in keywords if k["position"] is not None or k["best"] is not None],
            key=lambda k: (k["position"] if k["position"] is not None else 1000, k["best"] or 1000))[:5]],
        "movers": movers, "new_keywords": new_keywords[:20],
        "traffic": traffic, "distribution": distribution, "speed": speed, "pages": page_out,
    }
