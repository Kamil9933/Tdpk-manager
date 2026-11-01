"""
SEO approval queue: turns the draft rows the GitHub Actions SEO report writes
into seo_suggestions (seo/seo_report.py) into changes you approve one by one
in the dashboard's SEO tab.

Flow for a suggestion about a product or collection (low_ctr,
striking_distance):
  1. "Draft copy" -- Gemini writes a new SEO title + meta description from
     the page's current ones and the suggestion's data. Saved on the row,
     nothing changes on the store.
  2. You edit the text if you want, then "Approve & apply" -- this is the
     ONLY step that writes to Shopify, and only for that one product or
     collection. The previous title/description are kept on the row so a
     change can be reverted by hand.
Suggestions that need a human rather than copy (not_live, not_indexed,
slow, or a page that isn't a product/collection like the home page) can only
be marked done or rejected.
"""
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from core import gemini_fallback

TARGETS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seo", "targets.json")
TITLE_MAX, DESCRIPTION_MAX = 60, 155
COPY_KINDS = {"low_ctr", "striking_distance"}


def load_targets():
    with open(TARGETS_PATH, encoding="utf-8") as f:
        return json.load(f)


def resource_for(page_key, targets=None):
    """Which Shopify object a target page is: product / collection by handle,
    or None for pages the API can't set SEO on from here (home page, pages)."""
    page = next((p for p in (targets or load_targets())["pages"] if p["key"] == page_key), None)
    if not page:
        return None
    parts = [p for p in urlparse(page["url"]).path.split("/") if p]
    kind = {"products": "product", "collections": "collection"}.get(parts[0]) if len(parts) == 2 else None
    return {"kind": kind, "handle": parts[1] if kind else None, "url": page["url"], "name": page["name"]}


def can_apply(suggestion, resource):
    return bool(resource and resource["kind"] and suggestion.get("kind") in COPY_KINDS)


async def current_seo(shopify, resource):
    field = "productByIdentifier" if resource["kind"] == "product" else "collectionByIdentifier"
    data = await shopify.graphql(
        f"query($h: String!) {{ r: {field}(identifier: {{handle: $h}}) {{ id title seo {{ title description }} }} }}",
        {"h": resource["handle"]})
    r = (data.get("data") or {}).get("r")
    if not r:
        raise ValueError(f"{resource['kind']} '{resource['handle']}' not found in Shopify")
    return {"id": int(r["id"].rsplit("/", 1)[-1]), "name": r["title"],
            "title": (r.get("seo") or {}).get("title") or "", "description": (r.get("seo") or {}).get("description") or ""}


def _prompt(suggestion, resource, current):
    detail = suggestion.get("detail") or {}
    goal = (f'It ranks around position {detail.get("position")} for the search "{detail.get("query")}". '
            f'Work that phrase in naturally to move it onto page 1.') if suggestion["kind"] == "striking_distance" else \
           (f'It shows in Google often (position {detail.get("position")}) but few people click '
            f'({float(detail.get("ctr") or 0):.1%} CTR). Make the listing more compelling to click.')
    return (
        "You write SEO titles and meta descriptions for thirddimension.pk, a 3D-printing shop in Lahore, "
        "Pakistan, selling 3D-printed collectibles, lamps and custom prints nationwide with cash on delivery.\n"
        f"Page: {resource['name']} ({resource['kind']}: {current['name']}) {resource['url']}\n"
        f"Current SEO title: {current['title'] or '(none)'}\n"
        f"Current meta description: {current['description'] or '(none)'}\n"
        f"Goal: {goal}\n"
        f"Rules: title at most {TITLE_MAX} characters, description at most {DESCRIPTION_MAX}. "
        "Only state facts already in the current text or obvious from the product name -- no invented "
        "prices, sizes, discounts or delivery times. Plain, natural English.\n"
        'Reply with JSON only: {"title": "...", "description": "..."}'
    )


async def draft_copy(suggestion, resource, current):
    text, model = await gemini_fallback.generate_text(_prompt(suggestion, resource, current))
    try:
        data = json.loads(text.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
    except ValueError:
        raise ValueError("Gemini didn't return valid JSON -- try drafting again") from None
    title, description = str(data.get("title") or "").strip(), str(data.get("description") or "").strip()
    if not title or not description:
        raise ValueError("Gemini returned an empty title or description -- try drafting again")
    return {"title": title[:TITLE_MAX], "description": description[:DESCRIPTION_MAX], "model": model}


async def apply_copy(shopify, resource, object_id, title, description):
    """Writes the SEO title/description to ONE product or collection. REST
    metafields_global_* fields, which behave the same across API versions."""
    seo = {"metafields_global_title_tag": title, "metafields_global_description_tag": description}
    if resource["kind"] == "product":
        await shopify._put(f"/products/{object_id}.json", {"product": {"id": object_id, **seo}})
        return
    # Collections are either custom or smart; only one of these exists, so a
    # 404 on the first means try the second. Any other error is real.
    try:
        await shopify._put(f"/custom_collections/{object_id}.json", {"custom_collection": {"id": object_id, **seo}})
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        await shopify._put(f"/smart_collections/{object_id}.json", {"smart_collection": {"id": object_id, **seo}})


def now_iso():
    return datetime.now(timezone.utc).isoformat()
