"""Editing engine, jobs, captions and carousel publishing, on small synthetic
photos and clips. Video tests need ffmpeg (imageio-ffmpeg in requirements);
they're skipped if it can't be found."""
import asyncio
import os
import re
import shutil
import subprocess
import types
from io import BytesIO

import pytest
from PIL import Image

from core import media_edit as me
from core import media_jobs, captions, rules
from core import supabase_client as sbc
from core.meta import MetaClient, _strip_paging_urls
from core.mcp_server import build_mcp_server


def jpeg(size=(3000, 2000), color=(60, 40, 90), exif_gps=False):
    img = Image.new("RGB", size, color)
    out = BytesIO()
    if exif_gps:
        exif = Image.Exif()
        exif[0x8825] = {1: "N", 2: (31.0, 31.0, 0.0)}  # GPSInfo: somewhere in Lahore
        img.save(out, "JPEG", exif=exif)
    else:
        img.save(out, "JPEG")
    return out.getvalue()


def opened(data):
    return Image.open(BytesIO(data))


# ── images ──

@pytest.mark.parametrize("preset", me.IMAGE_PRESETS)
def test_every_image_preset_outputs_exact_size_jpeg(preset):
    data, warnings = me.edit_image(jpeg(), preset, {"headline": "Take F1\nEverywhere", "subtext": "Rs 1,200"})
    img = opened(data)
    assert img.format == "JPEG" and img.size == me.PRESETS[preset]
    assert len(data) <= me.IG_IMAGE_MAX_BYTES and warnings == []


@pytest.mark.parametrize("fit", ["crop", "blur", "solid"])
def test_fits_keep_the_ratio(fit):
    data, _ = me.edit_image(jpeg((1200, 3000)), "feed", {"fit": fit})
    assert opened(data).size == (1080, 1350)


def test_gps_exif_is_stripped():
    data, _ = me.edit_image(jpeg(exif_gps=True), "feed", {})
    assert 0x8825 not in opened(data).getexif()


def test_low_res_is_not_blown_up_and_warns():
    data, warnings = me.edit_image(jpeg((500, 500)), "feed", {})
    w, h = opened(data).size
    assert w < 1080 and abs(w / h - 0.8) < 0.01
    assert "low resolution" in warnings[0]


def test_crop_box_follows_focus_within_edges():
    assert me.crop_box((2000, 1000), 1.0, (0.5, 0.5)) == (500, 0, 1500, 1000)
    assert me.crop_box((2000, 1000), 1.0, (0.0, 0.5)) == (0, 0, 1000, 1000)
    assert me.crop_box((2000, 1000), 1.0, (1.0, 0.5)) == (1000, 0, 2000, 1000)


@pytest.mark.parametrize("size", [(1080, 1350), (1080, 1920), (1080, 1080)])
def test_overlays_stay_in_safe_area(size):
    opts = me.merge_options("feed", "image", {"headline": "Upgrade your\nBatcave", "subtext": "Made in Lahore"})
    _, placed = me.overlay_layer(size, opts, {})
    w, h = size
    top, bottom = me._margins(size)
    assert set(placed) == {"logo", "headline", "subtext", "footer"}
    assert placed["logo"][1] >= top - 1
    assert placed["footer"][3] <= h - bottom + 5
    for box in placed.values():
        assert 0 <= box[0] and box[2] <= w
    if h / w > 1.6:  # Reel: clear of Instagram's bottom UI
        assert placed["footer"][3] <= h * 0.84


def test_overlay_uses_uploaded_logo(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (400, 200), (255, 0, 0, 255)).save(logo)
    layer, placed = me.overlay_layer((1080, 1350), me.merge_options("feed", "image", {}), {"logo": str(logo)})
    x0, y0, x1, y1 = placed["logo"]
    assert x1 - x0 == round(1080 * 0.09)
    assert layer.getpixel(((x0 + x1) // 2, (y0 + y1) // 2))[:3] == (255, 0, 0)


def test_overlays_can_be_switched_off():
    _, placed = me.overlay_layer((1080, 1350), me.merge_options("feed", "image", {"logo": False, "footer": False}), {})
    assert placed == {}


def test_carousel_normalises_every_slide():
    outs, _ = me.edit_carousel([jpeg((3000, 2000)), jpeg((1500, 2500)), jpeg((2000, 2000))], "carousel", {})
    assert {opened(o).size for o in outs} == {(1080, 1080)}
    with pytest.raises(me.EditError, match="2 to 10"):
        me.edit_carousel([jpeg()], "carousel", {})


def test_bad_options_rejected():
    with pytest.raises(me.EditError):
        me.edit_image(jpeg(), "poster", {})
    with pytest.raises(me.EditError):
        me.merge_options("reel", "video", {"fit": "stretch"})
    with pytest.raises(me.EditError):
        me.merge_options("reel", "video", {"speed": 50})


# ── video planning (no ffmpeg needed) ──

def pr(w=1080, h=1920, d=15.0, audio=True):
    return {"width": w, "height": h, "duration": d, "audio": audio}


def test_plan_reel_basics():
    p = me.plan_video([pr()], "reel", me.merge_options("reel", "video", {}))
    assert p["size"] == (1080, 1920) and p["duration"] == 15 and p["warnings"] == []
    assert p["video_kbps"] * p["duration"] / 8 / 1000 < 48  # fits the size cap


def test_plan_limits():
    o = me.merge_options("reel", "video", {})
    with pytest.raises(me.EditError, match="under 90s"):
        me.plan_video([pr(d=120)], "reel", o)
    with pytest.raises(me.EditError, match="at least 3s"):
        me.plan_video([pr(d=2)], "reel", o)
    with pytest.raises(me.EditError, match="limit is 10"):
        me.plan_video([pr(d=700)], "reel", o)
    with pytest.raises(me.EditError, match="1 to 10 clips"):
        me.plan_video([pr()] * 11, "reel", o)


def test_plan_timelapse_and_trims():
    p = me.plan_video([pr(d=60)], "timelapse", me.merge_options("timelapse", "video", {}))
    assert p["duration"] == 15
    o = me.merge_options("reel", "video", {"trims": [[2, 8], [0, 0]]})
    p = me.plan_video([pr(d=20), pr(d=5)], "reel", o)
    assert p["duration"] == 11


def test_plan_landscape_source_warns_instead_of_upscaling():
    p = me.plan_video([pr(1920, 1080)], "reel", me.merge_options("reel", "video", {}))
    assert p["size"][0] < 1080 and p["size"][0] % 2 == 0 and p["warnings"]


def test_parse_probe_rotation_and_audio():
    text = ("  Duration: 00:00:12.50, start: 0.000000, bitrate: 9000 kb/s\n"
            "  Stream #0:0[0x1](und): Video: h264 (High) (avc1), yuv420p, 1920x1080, 30 fps\n"
            "      Side data:\n        displaymatrix: rotation of -90.00 degrees\n"
            "  Stream #0:1[0x2](und): Audio: aac (LC), 44100 Hz, stereo\n")
    assert me.parse_probe(text) == {"duration": 12.5, "width": 1080, "height": 1920, "audio": True}


def test_progress_lines():
    assert me.progress_from_line("out_time_us=5000000", 10) == 50
    assert me.progress_from_line("out_time_us=99000000", 10) == 99
    assert me.progress_from_line("frame=10", 10) is None


# ── video rendering (real ffmpeg) ──

try:
    FFMPEG = me.ffmpeg_exe()
except me.EditError:
    FFMPEG = None
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")


def make_clip(path, size="720x1280", seconds=4, audio=True):
    args = [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest", "-c:a", "aac"]
    subprocess.run(args + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return str(path)


def render(tmp_path, sources, preset, opts, music=None):
    o = me.merge_options(preset, "video", opts)
    plan = me.plan_video([me.probe(s) for s in sources], preset, o)
    ov = me.write_overlay_png(plan["size"], o, {}, str(tmp_path / "ov.png"))
    out = str(tmp_path / f"{preset}.mp4")
    subprocess.run(me.build_video_command(sources, plan, o, ov, music, out, exe=FFMPEG), check=True, capture_output=True)
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", out], capture_output=True, text=True).stderr
    return out, plan, info


@needs_ffmpeg
def test_reel_output_spec(tmp_path):
    src = make_clip(tmp_path / "a.mp4")
    out, plan, info = render(tmp_path, [src], "reel", {"headline": "Light up\nyour world"})
    assert re.search(r"Video: h264.*yuv420p.*1080x1920.*30 fps", info)
    assert "Audio: aac" in info
    assert os.path.getsize(out) < 50 * 1024 * 1024
    assert abs(me.probe(out)["duration"] - 4) < 0.2
    with open(out, "rb") as f:  # faststart: moov atom before mdat
        head = f.read(200_000)
    assert head.find(b"moov") < head.find(b"mdat")


@needs_ffmpeg
def test_timelapse_join_and_music(tmp_path):
    a = make_clip(tmp_path / "a.mp4", seconds=8)
    b = make_clip(tmp_path / "b.mp4", size="1280x720", seconds=6, audio=False)
    music = str(tmp_path / "m.mp3")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=880:duration=2", music], check=True)
    out, plan, info = render(tmp_path, [a, b], "timelapse", {"fit": "blur"}, music=music)
    assert abs(me.probe(out)["duration"] - 14 / 4) < 0.2
    assert "Audio: aac" in info  # music looped under the silent sped-up clips


@needs_ffmpeg
@pytest.mark.parametrize("preset,size", [("facebook", "1080x1350"), ("square", "1080x1080")])
def test_other_video_presets(tmp_path, preset, size):
    src = make_clip(tmp_path / "a.mp4", size="1080x1920", seconds=3)
    _, _, info = render(tmp_path, [src], preset, {"fit": "solid"})
    assert size in info


# ── jobs (fake storage, real engine) ──

class FakeDB:
    def __init__(self, items, tmp):
        self.items, self.jobs, self.uploads, self.deleted, self.tmp = items, {}, [], [], tmp
    async def get_media(self, p): return self.items.get(p)
    async def list_media(self): return list(self.items.values())
    async def create_media_job(self, data):
        job = {"id": f"j{len(self.jobs) + 1}", "status": "queued", "progress": 0, "result_paths": [], "warnings": [], **data}
        self.jobs[job["id"]] = job
        return job
    async def get_media_job(self, i): return self.jobs.get(i)
    async def update_media_job(self, i, data): self.jobs[i].update(data)
    async def list_media_jobs(self, limit=20): return list(self.jobs.values())[::-1][:limit]
    async def fetch_media_assets(self, folder, music=None): return {}
    async def upload_media(self, body, filename, content_type):
        path = f"new-{len(self.uploads) + 1}-{filename}"
        self.uploads.append((path, content_type, len(body)))
        self.items[path] = {"path": path, "public_url": f"file://{path}", "kind": "video" if content_type.startswith("video") else "image"}
        return {"path": path}
    async def delete_media(self, p): self.deleted.append(p)


@pytest.fixture
def jobdb(tmp_path, monkeypatch):
    src = tmp_path / "photo.jpg"
    src.write_bytes(jpeg())
    items = {f"p{i}.jpg": {"path": f"20261103-120000-abc123-batman-lamp-{i}.jpg", "public_url": str(src), "kind": "image"} for i in range(3)}
    items = {v["path"]: v for v in items.values()}
    items["clip.mp4"] = {"path": "clip.mp4", "public_url": "x", "kind": "video"}
    async def fake_download(url, path):
        shutil.copy(url, path)
        return path
    monkeypatch.setattr(media_jobs, "_download", fake_download)
    return FakeDB(items, tmp_path)


def run_job(db, kind, preset, paths, params=None):
    async def go():
        job = await media_jobs.start(db, kind, preset, paths, params or {})
        await asyncio.gather(*media_jobs._TASKS)
        return db.jobs[job["id"]]
    return asyncio.run(go())


def test_image_job_makes_new_file_and_keeps_original(jobdb):
    src = next(p for p in jobdb.items if p.endswith(".jpg"))
    job = run_job(jobdb, "image", "feed", [src])
    assert job["status"] == "done" and job["progress"] == 100
    assert job["result_paths"] == ["new-1-batman-lamp-0-feed.jpg"]
    assert src in jobdb.items and jobdb.deleted == []


def test_carousel_job(jobdb):
    srcs = [p for p in jobdb.items if p.endswith(".jpg")]
    job = run_job(jobdb, "carousel", "carousel", srcs)
    assert len(job["result_paths"]) == 3 and job["status"] == "done"


def test_job_validation(jobdb):
    src = next(p for p in jobdb.items if p.endswith(".jpg"))
    for kind, preset, paths, msg in [
        ("video", "reel", [src], "video clips only"),
        ("carousel", "carousel", [src], "2 to 10"),
        ("image", "feed", ["missing.jpg"], "No uploaded file"),
        ("image", "reel", [src], "Unknown image preset"),
    ]:
        with pytest.raises(me.EditError, match=msg):
            asyncio.run(media_jobs.start(jobdb, kind, preset, paths, {}))
    assert jobdb.jobs == {}


def test_failed_job_records_reason(jobdb, monkeypatch):
    src = next(p for p in jobdb.items if p.endswith(".jpg"))
    monkeypatch.setattr(me, "edit_image", lambda *a: (_ for _ in ()).throw(me.EditError("That image could not be read.")))
    job = run_job(jobdb, "image", "feed", [src])
    assert job["status"] == "failed" and job["error"] == "That image could not be read."


def test_agent_tools_edit_latest_and_status(jobdb):
    server = build_mcp_server(None, None, jobdb, None, resolve_scope=None)
    import json
    def call(name, args):
        res = asyncio.run(server.call_tool(name, args))
        content = res[0] if isinstance(res, tuple) else res
        return json.loads("\n".join(getattr(c, "text", "") for c in content))
    out = call("edit_image", {"media_id": "latest_image", "preset": "square"})
    assert out["status"] == "done" and out["results"][0]["media_id"].endswith("-square.jpg")
    assert call("get_media_job", {"job_id": out["job_id"]})["status"] == "done"
    assert "error" in call("edit_image", {"media_id": "clip.mp4"})
    reply = asyncio.run(rules.try_handle(server, None, "c1", "edit status"))
    assert "square image" in reply and "done" in reply


# ── assets ──

def png_bytes(mode):
    out = BytesIO()
    Image.new(mode, (10, 10)).save(out, "PNG")
    return out.getvalue()


def test_asset_paths_and_validation():
    assert sbc.media_asset_path("logo", "TD Logo.PNG", png_bytes("RGBA")) == ("logo.png", "image/png")
    with pytest.raises(sbc.MediaError, match="no transparency"):
        sbc.media_asset_path("logo", "logo.png", png_bytes("RGB"))
    assert sbc.media_asset_path("font", "Brush.OTF", b"x") == ("font.otf", "font/otf")
    assert sbc.media_asset_path("music", "My Beat (v2).mp3", b"x") == ("music/my-beat-v2.mp3", "audio/mpeg")
    with pytest.raises(sbc.MediaError):
        sbc.media_asset_path("music", "x.exe", b"x")


# ── captions ──

def test_caption_in_account_style():
    d = captions.draft_caption("Batman Signal Lamp", "2500", question="Which size should we print next?")
    c = d["caption"]
    assert c.startswith("Gotham called.") and "Batman Signal Lamp , Rs 2,500" in c
    assert "Which size should we print next?" in c and "Shop at thirddimension.pk" in c
    assert "\n.\n.\n" in c and "#batman" in d["hashtags"] and "#thirddimensionpk" in d["hashtags"]
    assert len(d["hashtags"]) <= 12 and "—" not in c


def test_caption_without_hashtags_or_known_theme():
    d = captions.draft_caption("Vase", hashtags=False)
    assert d["hashtags"] == [] and "#" not in d["caption"]


# ── Meta: carousel + token stripping ──

def test_paging_urls_with_token_are_removed():
    body = {"data": [], "paging": {"cursors": {"after": "x"}, "next": "https://graph...&access_token=SECRET"}}
    assert "SECRET" not in str(_strip_paging_urls(body)) and body["paging"]["cursors"]


def test_ig_carousel_flow(monkeypatch):
    c = MetaClient()
    c.ig_user_id, c.token = "ig1", "t"
    calls = []
    async def fake_call(method, path, params=None, token=None):
        calls.append((method, path, dict(params or {})))
        if path == "ig1/media":
            return {"id": f"c{len(calls)}"}
        if method == "GET":
            return {"status_code": "FINISHED"}
        return {"id": "post"}
    monkeypatch.setattr(c, "_call", fake_call)
    assert asyncio.run(c.publish_ig_carousel(["u1", "u2"], "cap")) == {"id": "post"}
    children = [p for m, path, p in calls if p.get("is_carousel_item")]
    parent = next(p for m, path, p in calls if p.get("media_type") == "CAROUSEL")
    assert len(children) == 2 and parent["children"] == "c1,c2" and parent["caption"] == "cap"
    assert calls[-1][1] == "ig1/media_publish"
