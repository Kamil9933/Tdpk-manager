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
import asyncio
import httpx


class MetaError(Exception):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


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
            msg = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
            raise MetaError(f"Meta API error: {msg or f'HTTP {r.status_code}'}", status=r.status_code, body=body)
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

    # ---------- ads: write ----------

    async def set_status(self, object_id, status):
        """status is 'ACTIVE' or 'PAUSED'. Affects live ad delivery/spend."""
        return await self._call("POST", object_id, {"status": status})

    async def set_daily_budget(self, adset_id, amount_minor):
        """amount_minor is the smallest currency unit (cents for USD)."""
        return await self._call("POST", adset_id, {"daily_budget": amount_minor})

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
