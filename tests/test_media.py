"""Social media storage: upload validation, PNG->JPEG, paths and URLs,
cleanup cutoff, the publish gate (preview first) and Reel polling. Supabase
Storage and Meta are faked."""
import asyncio
import json
import types
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from PIL import Image

from core import supabase_client as sbc
from core import media, rules
from core.meta import MetaClient, MetaError
from core.mcp_server import build_mcp_server

URL = "https://proj.supabase.co"


@pytest.fixture(autouse=True)
def supabase_url(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", URL)


def png(mode="RGBA", color=(255, 0, 0, 0)):
    out = BytesIO()
    Image.new(mode, (4, 4), color).save(out, "PNG")
    return out.getvalue()


# ── validation and conversion ──

def test_rejects_unsupported_type():
    with pytest.raises(sbc.MediaError, match="Unsupported file type 'image/gif'"):
        sbc.prepare_media(b"GIF89a", "image/gif")


def test_rejects_empty_and_oversize():
    with pytest.raises(sbc.MediaError, match="empty"):
        sbc.prepare_media(b"", "image/jpeg")
    with pytest.raises(sbc.MediaError, match="limit is 50 MB"):
        sbc.prepare_media(b"x" * (sbc.MEDIA_MAX_BYTES + 1), "video/mp4")


def test_png_becomes_jpeg_on_white():
    body, ct, ext = sbc.prepare_media(png(), "image/png")
    assert (ct, ext) == ("image/jpeg", "jpg")
    img = Image.open(BytesIO(body))
    assert img.format == "JPEG" and img.mode == "RGB"
    assert min(img.getpixel((1, 1))) > 240  # transparent -> white, not black


def test_damaged_png_gives_clear_error():
    with pytest.raises(sbc.MediaError, match="could not be read"):
        sbc.prepare_media(b"\x89PNG not really", "image/png")


def test_jpeg_and_video_pass_through_untouched():
    assert sbc.prepare_media(b"jpegbytes", "image/jpeg") == (b"jpegbytes", "image/jpeg", "jpg")
    assert sbc.prepare_media(b"movbytes", "video/quicktime") == (b"movbytes", "video/quicktime", "mov")


def test_content_type_falls_back_to_extension():
    assert sbc.media_content_type("application/octet-stream", "clip.MOV") == "video/quicktime"
    assert sbc.media_content_type("image/jpg", "a.jpg") == "image/jpeg"
    assert sbc.media_content_type("", "a.gif") == ""


# ── paths and URLs ──

def test_path_is_unique_and_sanitised():
    now = datetime(2026, 11, 3, 14, 15, 2, tzinfo=timezone.utc)
    p = sbc.media_path("../Batman Lamp (final)!.PNG", "jpg", now=now)
    assert p.startswith("20261103-141502-") and p.endswith("-batman-lamp-final.jpg")
    assert "/" not in p and ".." not in p
    assert sbc.media_path("x.jpg", "jpg") != sbc.media_path("x.jpg", "jpg")
    assert sbc.media_path("###.mp4", "mp4", now=now).endswith("-media.mp4")


def test_public_url():
    assert sbc.media_public_url("a.jpg") == f"{URL}/storage/v1/object/public/social-media/a.jpg"


# ── storage calls (fake bucket) ──

class FakeBucket:
    def __init__(self, rows=()):
        self.rows, self.uploads, self.removed = list(rows), [], []
    def upload(self, path, body, opts): self.uploads.append((path, body, opts))
    def list(self, path, opts): return self.rows
    def remove(self, paths): self.removed.extend(paths); return []


@pytest.fixture
def bucket(monkeypatch):
    b = FakeBucket()
    storage = types.SimpleNamespace(from_=lambda name: b if name == "social-media" else None)
    monkeypatch.setattr(sbc, "get_client", lambda: types.SimpleNamespace(storage=storage))
    return b


def test_upload_returns_url_and_converts_png(bucket):
    out = asyncio.run(sbc.Database().upload_media(png(), "Shelf.png", "image/png"))
    path, body, opts = bucket.uploads[0]
    assert out["path"] == path and path.endswith("-shelf.jpg")
    assert out["public_url"] == sbc.media_public_url(path)
    assert out["content_type"] == opts["content-type"] == "image/jpeg"
    assert out["size"] == len(body)


def test_list_skips_folders_and_marks_videos(bucket):
    bucket.rows = [
        {"name": "a.mp4", "id": "1", "created_at": "2026-11-01T00:00:00Z", "metadata": {"size": 10, "mimetype": "video/mp4"}},
        {"name": "folder", "id": None},
    ]
    items = asyncio.run(sbc.Database().list_media())
    assert [(m["path"], m["kind"]) for m in items] == [("a.mp4", "video")]


def test_cleanup_cutoff(bucket):
    now = datetime.now(timezone.utc)
    bucket.rows = [
        {"name": "old.jpg", "id": "1", "created_at": (now - timedelta(days=8)).isoformat()},
        {"name": "edge.jpg", "id": "2", "created_at": (now - timedelta(days=6, hours=23)).isoformat()},
        {"name": "new.jpg", "id": "3", "created_at": now.isoformat()},
        {"name": "unknown.jpg", "id": "4", "created_at": None},
    ]
    assert asyncio.run(sbc.Database().purge_old_media(7)) == ["old.jpg"]
    assert bucket.removed == ["old.jpg"]


def test_cleanup_with_nothing_old_deletes_nothing(bucket):
    bucket.rows = [{"name": "new.jpg", "id": "3", "created_at": datetime.now(timezone.utc).isoformat()}]
    assert asyncio.run(sbc.Database().purge_old_media(7)) == []
    assert bucket.removed == []


# ── publishing ──

IMG = {"path": "a.jpg", "public_url": f"{URL}/storage/v1/object/public/social-media/a.jpg", "kind": "image"}
VID = {"path": "b.mp4", "public_url": f"{URL}/storage/v1/object/public/social-media/b.mp4", "kind": "video"}


class FakeDB:
    async def get_media(self, path): return {"a.jpg": IMG, "b.mp4": VID}.get(path)
    async def list_media(self): return [IMG, VID]
    async def get_automation_settings(self): return {"social_publish": {"enabled": True, "config": {}}}


class FakeMeta:
    def __init__(self): self.calls = []
    async def publish_ig_image(self, url, caption): self.calls.append(("ig_image", url)); return {"id": "1"}
    async def publish_ig_reel(self, url, caption): self.calls.append(("ig_reel", url)); return {"id": "2"}
    async def post_to_page(self, message, image_url=None, link=None, schedule_at=None): self.calls.append(("fb_photo", image_url)); return {"id": "3"}
    async def post_video_to_page(self, url, description): self.calls.append(("fb_video", url)); return {"id": "4"}


def test_resolve_by_id_url_and_foreign_url():
    db = FakeDB()
    assert asyncio.run(media.resolve(db, media_id="a.jpg")) is IMG
    assert asyncio.run(media.resolve(db, url=VID["public_url"])) is VID
    assert asyncio.run(media.resolve(db, url="https://elsewhere.com/x.jpg")) is None
    with pytest.raises(LookupError):
        asyncio.run(media.resolve(db, media_id="gone.jpg"))


def test_publish_routes_by_kind():
    m = FakeMeta()
    for item in (IMG, VID):
        for target in media.TARGETS:
            asyncio.run(media.publish(m, item, target, "hi"))
    assert [c[0] for c in m.calls] == ["ig_image", "fb_photo", "ig_reel", "fb_video"]


def call(server, name, args):
    res = asyncio.run(server.call_tool(name, args))
    content = res[0] if isinstance(res, tuple) else res
    return json.loads("\n".join(getattr(c, "text", "") for c in content))


@pytest.fixture
def server():
    m = FakeMeta()
    return build_mcp_server(None, None, FakeDB(), m, resolve_scope=None), m


def test_tools_preview_first_even_with_autonomy_on(server):
    srv, m = server
    assert call(srv, "publish_instagram_image", {"caption": "c", "media_id": "a.jpg"})["preview"]
    assert call(srv, "publish_instagram_reel", {"caption": "c", "media_id": "b.mp4"})["preview"]
    assert call(srv, "post_to_facebook", {"message": "c", "media_id": "b.mp4"})["preview"]
    assert m.calls == []


def test_tools_publish_uploaded_media_on_confirm(server):
    srv, m = server
    assert call(srv, "publish_instagram_reel", {"caption": "c", "video_url": VID["public_url"], "confirm": True})["ok"]
    assert call(srv, "publish_instagram_image", {"caption": "c", "image_url": "https://elsewhere.com/x.jpg", "confirm": True})["ok"]
    assert m.calls == [("ig_reel", VID["public_url"]), ("ig_image", "https://elsewhere.com/x.jpg")]


def test_tools_reject_wrong_kind_and_missing_source(server):
    srv, m = server
    assert "use publish_instagram_reel" in call(srv, "publish_instagram_image", {"caption": "c", "media_id": "b.mp4", "confirm": True})["error"]
    assert "media_id" in call(srv, "publish_instagram_image", {"caption": "c"})["error"]
    assert m.calls == []


def test_media_tools_and_tier1(server):
    srv, _ = server
    assert call(srv, "get_media_url", {"media_id": "a.jpg"})["public_url"] == IMG["public_url"]
    assert "error" in call(srv, "get_media_url", {"media_id": "nope"})
    reply = asyncio.run(rules.try_handle(srv, None, "c1", "media"))
    assert "2 uploaded file(s)" in reply and "a.jpg" in reply
    assert "media --" in rules.HELP_TEXT


# ── Reel polling ──

def reel_client(statuses, monkeypatch):
    c = MetaClient()
    c.ig_user_id, c.token = "ig1", "t"
    c.REEL_POLL_SECONDS, c.REEL_TIMEOUT_SECONDS = 1, 3
    calls, seq = [], iter(statuses)
    async def fake_call(method, path, params=None, token=None):
        calls.append((method, path))
        if path == "ig1/media":
            return {"id": "cont1"}
        if path == "cont1":
            return {"status_code": next(seq)}
        return {"id": "post1"}
    monkeypatch.setattr(c, "_call", fake_call)
    async def no_sleep(s): pass
    monkeypatch.setattr("core.meta.asyncio.sleep", no_sleep)
    return c, calls


def test_reel_waits_for_finished_then_publishes(monkeypatch):
    c, calls = reel_client(["IN_PROGRESS", "FINISHED"], monkeypatch)
    assert asyncio.run(c.publish_ig_reel("u", "c")) == {"id": "post1"}
    assert calls[-1] == ("POST", "ig1/media_publish")


def test_reel_timeout_never_publishes(monkeypatch):
    c, calls = reel_client(["IN_PROGRESS"] * 10, monkeypatch)
    with pytest.raises(MetaError, match="still processing"):
        asyncio.run(c.publish_ig_reel("u", "c"))
    assert ("POST", "ig1/media_publish") not in calls


def test_reel_error_status(monkeypatch):
    c, calls = reel_client(["ERROR"], monkeypatch)
    with pytest.raises(MetaError, match="could not process"):
        asyncio.run(c.publish_ig_reel("u", "c"))
    assert ("POST", "ig1/media_publish") not in calls
