"""
Meta Graph API client -- ads (existing campaigns/ad sets only, see mcp_server.py
docstring for why campaign *creation* is a separate phase), Facebook Page posts,
and Instagram (posts, comments, insights).

Ported from the working local tdpk-meta-mcp tool (TypeScript) to live directly in
TDPK's own backend, so it works from the dashboard/MCP connector without your
desktop needing to be on. Same Graph API calls, same field selections, same
"system user token + discover business assets" approach -- see README notes
below for things that don't change across the port.

Auth model: a single long-lived Meta "system user" access token (from Business
Settings), plus a Page access token fetched on demand for Page/Instagram actions
(Instagram write access rides on the Page token, same as the original tool).

Guardrails: every write method here is "just do it" -- the confirm-before-act
pattern lives one layer up, in main.py's routes and mcp_server.py's tools, so
there's exactly one place that decides whether an action needs a human okay
first, not duplicated into this client too.
"""

import os
import json
import asyncio
import httpx


class MetaError(Exception):
    def __init__(self, message, status=None, body=None, user_msg=None, subcode=None, field=None):
        super().__init__(message)
        self.status = status
        self.body = body
        # Meta's error object carries much more than `message` -- error_user_msg
        # is usually the actually-useful, human-readable explanation (e.g. "This
        # ad set belongs to a campaign using campaign budget optimization...");
        # error_subcode and the blamed field name pin down exactly what Meta
        # rejected. All three used to be silently dropped by _call(), which made
        # every failure look like a bare "Invalid parameter" with no way to tell
        # WHY. Kept as attributes (not just folded into the message) so a caller
        # can act on them programmatically if needed, as well as display them.
        self.user_msg = user_msg
        self.subcode = subcode
        self.field = field


def _version():
    return os.getenv("META_API_VERSION", "v23.0")


def _base():
    return f"https://graph.facebook.com/{_version()}"


class MetaClient:
    def __init__(self):
        self.token = os.getenv("META_ACCESS_TOKEN", "")
        self.page_id = os.getenv("META_PAGE_ID", "")
        self.business_id = os.getenv("META_BUSINESS_ID", "")
        ad_account = os.getenv("META_AD_ACCOUNT_ID", "")
        self.ad_account_id = ad_account if ad_account.startswith("act_") else f"act_{ad_account}" if ad_account else ""
        self.ig_user_id = os.getenv("META_IG_USER_ID", "")
        # Needed on the ad set for an OUTCOME_SALES/OFFSITE_CONVERSIONS campaign
        # -- without it Meta rejects ad-set creation with "needs a promoted
        # object" (the pixel/event that tracks purchases). Find it via
        # meta_inspect on a known-working ad set (e.g. "First Ad Set")'s
        # promoted_object field if it's ever unset and this starts failing again.
        self.pixel_id = os.getenv("META_PIXEL_ID", "")

    def _require(self, value, name):
        if not value:
            raise MetaError(f"Missing {name} -- set it as an environment variable")
        return value

    async def _call(self, method, path, params=None, token=None):
        params = dict(params or {})
        params = {k: v for k, v in params.items() if v is not None}
        params["access_token"] = token or self._require(self.token, "META_ACCESS_TOKEN")

        async with httpx.AsyncClient(timeout=60) as client:
            if method == "GET":
                r = await client.get(f"{_base()}/{path}", params=params)
            elif method == "POST":
                r = await client.post(f"{_base()}/{path}", data=params)
            elif method == "DELETE":
                r = await client.delete(f"{_base()}/{path}", params=params)
            else:
                raise ValueError(f"Unsupported method {method}")

        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code >= 400 or (isinstance(body, dict) and body.get("error")):
            err = (body.get("error") or {}) if isinstance(body, dict) else {}
            msg = err.get("message")
            user_msg = err.get("error_user_msg")
            user_title = err.get("error_user_title")
            subcode = err.get("error_subcode")
            # Meta sometimes names the offending field under error_data.blame_field_specs
            # (a list of field-path lists, e.g. [["adset", "daily_budget"]]) -- surface
            # it when present instead of making the caller go dig through the raw body.
            blame = ((err.get("error_data") or {}).get("blame_field_specs") or [])
            field = ", ".join(".".join(spec) for spec in blame) if blame else None
            parts = [msg or f"HTTP {r.status_code}"]
            if user_title:
                parts.append(user_title)
            if user_msg:
                parts.append(user_msg)
            if field:
                parts.append(f"field: {field}")
            if subcode:
                parts.append(f"subcode: {subcode}")
            raise MetaError(
                "Meta API error: " + " -- ".join(parts),
                status=r.status_code, body=body, user_msg=user_msg, subcode=subcode, field=field,
            )
        return body

    # ---------- discovery / connection check ----------

    async def who_am_i(self):
        return await self._call("GET", "me", {"fields": "id,name"})

    async def token_info(self):
        r = await self._call("GET", "debug_token", {"input_token": self.token})
        d = r.get("data", {}) or {}
        return {
            "app_id": d.get("app_id"),
            "app_name": d.get("application"),
            "type": d.get("type"),
            "is_valid": d.get("is_valid"),
            "expires_at": "never" if d.get("expires_at") == 0 else d.get("expires_at"),
            "scopes": d.get("scopes"),
        }

    async def discover(self):
        """System users can't use me/adaccounts or me/accounts -- look up assets
        through the business portfolio instead, same as the original tool."""
        biz = self._require(self.business_id, "META_BUSINESS_ID")

        async def safe(coro):
            try:
                return await coro
            except Exception as e:
                return {"error": str(e)}

        owned_ads = await safe(self._call("GET", f"{biz}/owned_ad_accounts", {"fields": "id,name,account_status,currency", "limit": 50}))
        client_ads = await safe(self._call("GET", f"{biz}/client_ad_accounts", {"fields": "id,name,account_status,currency", "limit": 50}))
        owned_pages = await safe(self._call("GET", f"{biz}/owned_pages", {"fields": "id,name,instagram_business_account{id,username}", "limit": 50}))
        ig_accounts = await safe(self._call("GET", f"{biz}/instagram_business_accounts", {"fields": "id,username", "limit": 50}))
        page_direct = None
        if self.page_id:
            page_direct = await safe(self._call("GET", self.page_id, {"fields": "id,name,instagram_business_account{id,username}"}))
        return {
            "owned_ad_accounts": owned_ads, "client_ad_accounts": client_ads,
            "owned_pages": owned_pages, "ig_accounts": ig_accounts, "page_direct": page_direct,
        }

    async def check_connection(self):
        return {"me": await self.who_am_i(), "token": await self.token_info(), "assets": await self.discover()}

    # ---------- ads: read ----------

    async def list_campaigns(self, limit=25):
        return await self._call("GET", f"{self._require(self.ad_account_id, 'META_AD_ACCOUNT_ID')}/campaigns", {
            "fields": "id,name,status,effective_status,objective,daily_budget,lifetime_budget", "limit": limit,
        })

    async def list_adsets(self, campaign_id=None, limit=25):
        path = f"{campaign_id}/adsets" if campaign_id else f"{self._require(self.ad_account_id, 'META_AD_ACCOUNT_ID')}/adsets"
        return await self._call("GET", path, {
            "fields": "id,name,status,effective_status,daily_budget,lifetime_budget,campaign_id", "limit": limit,
        })

    async def get_insights(self, date_preset="last_7d", level="campaign", object_id=None):
        path = f"{object_id or self._require(self.ad_account_id, 'META_AD_ACCOUNT_ID')}/insights"
        return await self._call("GET", path, {
            "fields": "campaign_name,adset_name,ad_name,spend,impressions,reach,clicks,cpc,ctr,cpm,actions,action_values,purchase_roas",
            "date_preset": date_preset, "level": level, "limit": 100,
        })

    async def get_object_fields(self, object_id, fields):
        """Generic read of ANY Graph API object by id with whatever
        comma-separated fields are asked for -- e.g. an ad set's
        promoted_object, an ad's tracking_specs. For live debugging (what
        does a known-working object actually have configured) without
        needing a new purpose-built method every time a new field matters."""
        return await self._call("GET", object_id, {"fields": fields})

    # ---------- ads: write ----------

    async def set_status(self, object_id, status):
        """status is 'ACTIVE' or 'PAUSED'. Affects live ad delivery/spend."""
        return await self._call("POST", object_id, {"status": status})

    async def set_daily_budget(self, adset_id, amount_minor):
        """amount_minor is the smallest currency unit (cents for USD)."""
        return await self._call("POST", adset_id, {"daily_budget": amount_minor})

    # ---------- ads: create new campaign from scratch (Phase 2) ----------
    # Meta's object model is Campaign -> Ad Set -> Ad -> Creative, each with its
    # own required fields. This wraps the whole chain behind one call so the
    # confirm-gated route/tool one layer up only has to show ONE preview, not
    # four. If a later step in the chain fails, everything created so far is
    # left as-is (paused, so nothing spends) and its ids are returned in the
    # error so the caller can tell the user exactly what partially exists.

    # A reasonable default optimization goal per objective, so the user doesn't
    # have to understand Meta's full goal taxonomy just to launch a campaign.
    OBJECTIVE_OPTIMIZATION_DEFAULTS = {
        "OUTCOME_TRAFFIC": "LINK_CLICKS",
        "OUTCOME_ENGAGEMENT": "POST_ENGAGEMENT",
        "OUTCOME_SALES": "OFFSITE_CONVERSIONS",
        "OUTCOME_AWARENESS": "REACH",
        "OUTCOME_LEADS": "LEAD_GENERATION",
        "OUTCOME_APP_PROMOTION": "APP_INSTALLS",
    }

    async def create_campaign(self, name, objective, daily_budget_minor, status="PAUSED", bid_strategy="LOWEST_COST_WITHOUT_CAP"):
        """Budget lives HERE, at the campaign, not on the ad set -- this is
        Advantage Campaign Budget (Meta's current name for what used to be
        called CBO). Verified against this ad account's own existing campaigns
        (via list_ad_campaigns/list_ad_sets): every campaign that works --
        including "First", the one known-good reference -- carries daily_budget
        at the campaign level with NO budget field on its ad set at all. A
        campaign created without a budget here is rejected outright by this
        account with a bare "Invalid parameter" (no budget = ad-set-budget mode,
        which this account doesn't allow for new campaigns), which is exactly
        the failure that prompted this fix. bid_strategy defaults to the
        no-cap option so a bid cap isn't silently required too."""
        return await self._call("POST", f"{self._require(self.ad_account_id, 'META_AD_ACCOUNT_ID')}/campaigns", {
            "name": name, "objective": objective, "status": status,
            "special_ad_categories": "[]",
            "daily_budget": daily_budget_minor, "bid_strategy": bid_strategy,
        })

    async def create_adset(self, campaign_id, name, optimization_goal,
                            countries, age_min=18, age_max=65, genders=None, status="PAUSED",
                            promoted_object=None):
        """No daily_budget/lifetime_budget here on purpose -- see create_campaign's
        docstring. An ad set under an Advantage-Campaign-Budget campaign (this
        account's convention) MUST NOT carry its own budget field; Meta rejects
        it with "Invalid parameter" the same way it rejects a budget-less
        campaign. This is also why set_ad_daily_budget fails on an EXISTING ad
        set in this account: the campaign already owns the budget, so an
        ad-set-level edit has nothing valid to change -- that's Meta correctly
        refusing an invalid operation, not a bug in this client. (genders is
        omitted entirely, not sent as "all" or [1,2], when not specified --
        Meta treats an absent genders field as unrestricted/all genders; the
        string "all" some logs show is only this app's own preview label, never
        sent to Meta's API.)

        promoted_object: required by Meta for an OFFSITE_CONVERSIONS
        optimization goal (the OUTCOME_SALES default) -- the pixel/event that
        tracks purchases. Omitting it is exactly what produced the "ad set
        needs a promoted object" error. Shape: {"pixel_id": "...",
        "custom_event_type": "PURCHASE"}, the standard setup for a Facebook
        Pixel tracking purchases, matching how "First Ad Set" (this account's
        known-working reference) is configured -- confirmed via meta_inspect
        rather than assumed.

        targeting_automation.advantage_audience is sent unconditionally
        (always 1) because this account requires it the same way it requires
        promoted_object -- "First Ad Set"'s own targeting (read live via
        meta_inspect) carries it, and the account rejects ad-set creation
        without it with a separate "Invalid parameter" (the next error after
        promoted_object was fixed). This is Meta's "Advantage+ Audience"
        (formerly Detailed Targeting Expansion), letting Meta expand beyond
        the explicit age/geo targeting when it predicts better performance --
        not something this account lets a new ad set opt out of."""
        targeting = {
            "geo_locations": {"countries": countries or ["PK"]}, "age_min": age_min, "age_max": age_max,
            "targeting_automation": {"advantage_audience": 1},
        }
        if genders:
            targeting["genders"] = genders
        params = {
            "name": name, "campaign_id": campaign_id,
            "billing_event": "IMPRESSIONS", "optimization_goal": optimization_goal,
            "targeting": json.dumps(targeting), "status": status,
        }
        if promoted_object:
            params["promoted_object"] = json.dumps(promoted_object)
        return await self._call("POST", f"{self._require(self.ad_account_id, 'META_AD_ACCOUNT_ID')}/adsets", params)

    async def create_ad_creative(self, name, message, link, image_url=None, cta="SHOP_NOW", page_id=None, instagram_actor_id=None):
        """instagram_actor_id lets the ad serve on Instagram placements using
        that IG account's identity, not just Facebook -- previously missing
        entirely. Defaults to META_IG_USER_ID (self.ig_user_id) so existing
        callers get it for free without passing anything new."""
        link_data = {"message": message, "link": link, "call_to_action": {"type": cta, "value": {"link": link}}}
        if image_url:
            link_data["picture"] = image_url
        object_story_spec = {"page_id": page_id or self._require(self.page_id, "META_PAGE_ID"), "link_data": link_data}
        ig_actor = instagram_actor_id or self.ig_user_id
        if ig_actor:
            object_story_spec["instagram_actor_id"] = ig_actor
        return await self._call("POST", f"{self._require(self.ad_account_id, 'META_AD_ACCOUNT_ID')}/adcreatives", {
            "name": name, "object_story_spec": json.dumps(object_story_spec),
        })

    async def create_ad(self, name, adset_id, creative_id, status="PAUSED"):
        return await self._call("POST", f"{self._require(self.ad_account_id, 'META_AD_ACCOUNT_ID')}/ads", {
            "name": name, "adset_id": adset_id, "creative": json.dumps({"creative_id": creative_id}), "status": status,
        })

    async def create_full_campaign(self, campaign_name, objective, daily_budget_minor, countries,
                                    message, link, image_url=None, cta="SHOP_NOW",
                                    age_min=18, age_max=65, genders=None, launch_active=False):
        """Creates Campaign -> Ad Set -> Creative -> Ad in sequence. Defaults to
        PAUSED (launch_active=False) as a second safety net on top of the
        confirm gate one layer up -- nothing spends until it's explicitly
        resumed from the Ads tab, same as any other ad."""
        status = "ACTIVE" if launch_active else "PAUSED"
        optimization_goal = self.OBJECTIVE_OPTIMIZATION_DEFAULTS.get(objective, "LINK_CLICKS")
        # OFFSITE_CONVERSIONS (the OUTCOME_SALES default) needs a promoted_object
        # -- the pixel/event tracking purchases -- or Meta rejects ad-set
        # creation with "needs a promoted object". META_PIXEL_ID mirrors how
        # "First Ad Set" (this account's known-working reference) is set up.
        promoted_object = None
        if optimization_goal == "OFFSITE_CONVERSIONS" and self.pixel_id:
            promoted_object = {"pixel_id": self.pixel_id, "custom_event_type": "PURCHASE"}
        progress = {}
        try:
            # Budget goes on the CAMPAIGN, not the ad set -- see create_campaign's
            # docstring. This is the actual fix for the "Invalid parameter" error:
            # this ad account requires Advantage Campaign Budget (every existing
            # campaign, including the known-working "First", carries its budget
            # here with none on the ad set).
            camp = await self.create_campaign(campaign_name, objective, daily_budget_minor, status=status)
            progress["campaign_id"] = camp["id"]
            adset = await self.create_adset(camp["id"], f"{campaign_name} - Ad Set",
                                             optimization_goal, countries, age_min, age_max, genders, status=status,
                                             promoted_object=promoted_object)
            progress["adset_id"] = adset["id"]
            creative = await self.create_ad_creative(f"{campaign_name} - Creative", message, link, image_url, cta)
            progress["creative_id"] = creative["id"]
            ad = await self.create_ad(f"{campaign_name} - Ad", adset["id"], creative["id"], status=status)
            progress["ad_id"] = ad["id"]
            return {"ok": True, "status": status, **progress}
        except MetaError as e:
            raise MetaError(
                f"{e} -- partially created (still PAUSED, nothing is spending): {progress or 'nothing yet'}. "
                f"You may need to finish or delete this in Meta Ads Manager directly.",
                status=e.status, body={"partial": progress, "original": e.body},
            )

    # ---------- facebook page ----------

    async def _page_token(self):
        r = await self._call("GET", self._require(self.page_id, "META_PAGE_ID"), {"fields": "access_token"})
        token = r.get("access_token")
        if not token:
            raise MetaError("Could not get a Page access token -- check the system user has the Page assigned.")
        return token

    async def list_page_posts(self, limit=10):
        return await self._call("GET", f"{self.page_id}/posts", {
            "fields": "id,message,created_time,permalink_url,is_published,scheduled_publish_time", "limit": limit,
        }, token=await self._page_token())

    async def post_to_page(self, message, image_url=None, link=None, schedule_at=None):
        """schedule_at is a unix timestamp (seconds), 10 minutes to 30 days ahead."""
        token = await self._page_token()
        scheduled = {"published": False, "scheduled_publish_time": schedule_at} if schedule_at else {}
        if image_url:
            return await self._call("POST", f"{self.page_id}/photos", {"url": image_url, "caption": message, **scheduled}, token=token)
        return await self._call("POST", f"{self.page_id}/feed", {"message": message, "link": link, **scheduled}, token=token)

    # ---------- instagram ----------

    async def list_ig_media(self, limit=12):
        return await self._call("GET", f"{self.ig_user_id}/media", {
            "fields": "id,caption,media_type,permalink,timestamp,like_count,comments_count", "limit": limit,
        })

    async def publish_ig_image(self, image_url, caption):
        """Two steps: create a container, then publish it. image_url must be public."""
        container = await self._call("POST", f"{self.ig_user_id}/media", {"image_url": image_url, "caption": caption})
        return await self._call("POST", f"{self.ig_user_id}/media_publish", {"creation_id": container["id"]})

    async def publish_ig_reel(self, video_url, caption):
        """video_url must be a public MP4. Video containers need processing time
        -- polls up to 150s (30 * 5s) before giving up, matching the original tool."""
        container = await self._call("POST", f"{self.ig_user_id}/media", {"media_type": "REELS", "video_url": video_url, "caption": caption})
        container_id = container["id"]
        for _ in range(30):
            status = await self._call("GET", container_id, {"fields": "status_code"})
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code == "ERROR":
                raise MetaError("Instagram could not process the video.")
            await asyncio.sleep(5)
        return await self._call("POST", f"{self.ig_user_id}/media_publish", {"creation_id": container_id})

    async def list_comments(self, media_id, limit=25):
        return await self._call("GET", f"{media_id}/comments", {"fields": "id,text,username,timestamp,like_count", "limit": limit})

    async def reply_to_comment(self, comment_id, message):
        return await self._call("POST", f"{comment_id}/replies", {"message": message})

    async def ig_insights(self, media_id):
        return await self._call("GET", f"{media_id}/insights", {"metric": "reach,likes,comments,shares,saved"})
