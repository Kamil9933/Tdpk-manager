"""
Publishing uploaded media (Supabase Storage bucket `social-media`) to
Instagram and Facebook. Shared by the dashboard's Media tab (main.py) and the
agent tools (mcp_server.py) so both publish the same way.

This module only acts; the preview-then-confirm gate stays in the callers,
same as core/meta.py.
"""
from core.supabase_client import media_public_url

TARGETS = ("instagram", "facebook")


async def resolve(db, media_id=None, url=None):
    """Finds an uploaded file by media_id (its storage path) or by its public
    URL. Returns the media item, or None for a URL that isn't one of ours
    (callers then publish that URL as-is, as before)."""
    if not media_id and url:
        prefix = media_public_url("")
        if url.startswith(prefix):
            media_id = url[len(prefix):]
    if not media_id:
        return None
    item = await db.get_media(media_id)
    if not item:
        raise LookupError(f"No uploaded file called '{media_id}'. Use list_media to see what's there.")
    return item


def action_label(item, target):
    if isinstance(item, list):
        return f"Publish {len(item)}-photo {'Instagram carousel' if target == 'instagram' else 'Facebook post'} NOW"
    if target == "instagram":
        return "Publish Instagram Reel NOW" if item["kind"] == "video" else "Publish Instagram image NOW"
    return "Publish Facebook video NOW" if item["kind"] == "video" else "Publish Facebook photo post NOW"


async def publish(meta, item, target, caption):
    """Images go to the feed, videos go out as Reels (Instagram) or Page
    videos (Facebook). A list of photos is a carousel / multi-photo post."""
    if isinstance(item, list):
        if any(i["kind"] != "image" for i in item) or not 2 <= len(item) <= 10:
            raise ValueError("A carousel is 2 to 10 photos.")
        urls = [i["public_url"] for i in item]
        if target == "instagram":
            return await meta.publish_ig_carousel(urls, caption)
        if target == "facebook":
            return await meta.post_photos_to_page(urls, caption)
        raise ValueError(f"target must be one of {', '.join(TARGETS)}")
    url, video = item["public_url"], item["kind"] == "video"
    if target == "instagram":
        return await (meta.publish_ig_reel(url, caption) if video else meta.publish_ig_image(url, caption))
    if target == "facebook":
        return await (meta.post_video_to_page(url, caption) if video else meta.post_to_page(caption, image_url=url))
    raise ValueError(f"target must be one of {', '.join(TARGETS)}")
