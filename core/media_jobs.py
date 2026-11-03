"""
Background edit jobs (Phase 4 add-on). An edit request returns a job id at
once; the work runs here in the server's own event loop, one job at a time
(Render's free instance has ~512 MB and a slow CPU), and the result is
uploaded as a NEW file in the social-media bucket. The source is never
touched. Progress and outcome live in the media_jobs table.

Free-tier caveat: Render stops an idle instance after ~15 minutes without
requests, which would kill a running job. The dashboard polls the job while
it runs, which keeps the instance awake; a job interrupted by a restart is
marked failed at the next startup (fail_stale_media_jobs).
"""
import asyncio
import os
import re
import tempfile

import httpx

from core import media_edit
from core.media_edit import EditError

_SEM = asyncio.Semaphore(1)
_TASKS = set()  # keep references so running tasks aren't garbage-collected


def _stem(path):
    """Original name without the timestamp/random prefix and extension."""
    stem = path.rsplit(".", 1)[0]
    return re.sub(r"^\d{8}-\d{6}-[0-9a-f]{6}-", "", stem) or "media"


async def start(db, kind, preset, source_paths, params):
    """Validates the request, records the job and starts it. Returns the job row."""
    if not source_paths:
        raise EditError("Pick at least one file.")
    items = []
    for p in source_paths:
        item = await db.get_media(p)
        if not item:
            raise EditError(f"No uploaded file called '{p}'.")
        items.append(item)
    kinds = {i["kind"] for i in items}
    if kind == "carousel":
        if kinds != {"image"}:
            raise EditError("Carousels are made from photos only.")
        if not 2 <= len(items) <= 10:
            raise EditError("A carousel needs 2 to 10 photos.")
    elif kind == "image":
        if kinds != {"image"} or len(items) != 1:
            raise EditError("Image edits take exactly one photo.")
        if preset not in media_edit.IMAGE_PRESETS:
            raise EditError(f"Unknown image preset '{preset}'. Use one of: {', '.join(media_edit.IMAGE_PRESETS)}.")
    elif kind == "video":
        if kinds != {"video"}:
            raise EditError("Video edits take video clips only.")
        if preset not in media_edit.VIDEO_PRESETS:
            raise EditError(f"Unknown video preset '{preset}'. Use one of: {', '.join(media_edit.VIDEO_PRESETS)}.")
    media_edit.merge_options(preset, kind, params)  # reject bad options now, not in the background
    job = await db.create_media_job({"kind": kind, "preset": preset, "params": params or {},
                                     "source_paths": list(source_paths)})
    task = asyncio.create_task(run(db, job["id"]))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return job


async def run(db, job_id):
    async with _SEM:
        job = await db.get_media_job(job_id)
        try:
            await db.update_media_job(job_id, {"status": "running", "progress": 2})
            with tempfile.TemporaryDirectory(prefix="tdpk-edit-") as tmp:
                result_paths, warnings = await _process(db, job, tmp)
            await db.update_media_job(job_id, {"status": "done", "progress": 100,
                                               "result_paths": result_paths, "warnings": warnings})
        except Exception as e:
            msg = str(e) if isinstance(e, EditError) else f"Edit failed: {e}"
            await db.update_media_job(job_id, {"status": "failed", "error": msg[:1000]})


async def _download(url, path):
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    f.write(chunk)
    return path


async def _process(db, job, tmp):
    kind, preset, params = job["kind"], job["preset"], job.get("params") or {}
    opts = media_edit.merge_options(preset, kind, params)
    sources = []
    for i, p in enumerate(job["source_paths"]):
        item = await db.get_media(p)
        if not item:
            raise EditError(f"'{p}' was deleted before the edit ran.")
        sources.append(await _download(item["public_url"], os.path.join(tmp, f"src{i}.{p.rsplit('.', 1)[-1]}")))
    assets = await db.fetch_media_assets(tmp, music=opts.get("music") if kind == "video" else None)
    await db.update_media_job(job["id"], {"progress": 10})
    name = _stem(job["source_paths"][0])

    if kind in ("image", "carousel"):
        datas = [open(s, "rb").read() for s in sources]
        if kind == "image":
            jpg, warnings = await asyncio.to_thread(media_edit.edit_image, datas[0], preset, params, assets)
            outs = [jpg]
        else:
            outs, warnings = await asyncio.to_thread(media_edit.edit_carousel, datas, preset, params, assets)
        paths = []
        for i, jpg in enumerate(outs):
            suffix = f"-{preset}" if kind == "image" else f"-carousel-{i + 1}"
            paths.append((await db.upload_media(jpg, f"{name}{suffix}.jpg", "image/jpeg"))["path"])
        return paths, warnings

    # video
    probes = [await asyncio.to_thread(media_edit.probe, s) for s in sources]
    plan = media_edit.plan_video(probes, preset, opts)
    overlay = media_edit.write_overlay_png(plan["size"], opts, assets, os.path.join(tmp, "overlay.png"))
    out = os.path.join(tmp, "out.mp4")
    args = media_edit.build_video_command(sources, plan, opts, overlay, assets.get("music"), out,
                                          exe=media_edit.ffmpeg_exe())
    await _run_ffmpeg(db, job["id"], args, plan["duration"])
    size = os.path.getsize(out)
    if size > 50 * 1024 * 1024:
        raise EditError(f"The result came out at {size / 1048576:.0f} MB, over the 50 MB limit. Trim it shorter.")
    with open(out, "rb") as f:
        stored = await db.upload_media(f.read(), f"{name}-{preset}.mp4", "video/mp4")
    return [stored["path"]], plan["warnings"]


async def _run_ffmpeg(db, job_id, args, duration):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stderr_tail, last = [], 0

    async def read_err():
        async for line in proc.stderr:
            stderr_tail.append(line.decode(errors="replace").rstrip())
            del stderr_tail[:-15]

    err_task = asyncio.create_task(read_err())
    async for raw in proc.stdout:
        pct = media_edit.progress_from_line(raw.decode(errors="replace").strip(), duration)
        if pct is not None and pct >= last + 5:
            last = pct
            await db.update_media_job(job_id, {"progress": max(10, pct)})
    rc = await proc.wait()
    await err_task
    if rc != 0:
        raise EditError("ffmpeg could not render the video: " + " | ".join(l for l in stderr_tail[-4:] if l))
