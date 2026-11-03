"""
Deterministic photo/video editing for social posts (Phase 4 add-on). No LLM
involved: presets + options in, a post-ready JPEG or MP4 out.

Images use Pillow; videos use ffmpeg (the binary bundled by imageio-ffmpeg,
or a system ffmpeg if one is on PATH). Text and logo overlays are drawn once
with Pillow as a transparent PNG and laid over the video by ffmpeg, so photos
and Reels share exactly the same look and ffmpeg needs no font support.

The brand look comes from core/brand_style.json (studied from
@thirddimension.pk's own posts). Defaults are deliberately light: small logo
and site footer, mild enhancement, a headline only when one is given.

Nothing here touches storage or the network: callers (core/media_jobs.py)
download the sources, call these functions, and upload the results as NEW
files. Originals are never overwritten.
"""
import json
import os
import re
import shutil
import subprocess
from io import BytesIO

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

BRAND = json.load(open(os.path.join(os.path.dirname(__file__), "brand_style.json"), encoding="utf-8"))

# name -> (width, height). Ratio follows from the size.
PRESETS = {
    "feed": (1080, 1350),       # Instagram feed, 4:5 (the account's standard)
    "square": (1080, 1080),     # 1:1
    "story": (1080, 1920),      # 9:16 story / Reel cover
    "reel": (1080, 1920),       # 9:16 Reel
    "facebook": (1080, 1350),   # Facebook feed, 4:5
    "landscape": (1920, 1080),  # 16:9
    "timelapse": (1080, 1920),  # 9:16 Reel, sped up
    "carousel": (1080, 1080),   # every slide the same 1:1 size
}
IMAGE_PRESETS = ("feed", "square", "story", "facebook", "landscape", "carousel")
VIDEO_PRESETS = ("reel", "story", "facebook", "feed", "square", "landscape", "timelapse")

MAX_UPSCALE = 1.5                  # beyond this, output smaller and warn instead
JPEG_QUALITY = 90
IG_IMAGE_MAX_BYTES = 8 * 1024 * 1024
VIDEO_MAX_BYTES = 48 * 1024 * 1024  # under the 50 MB bucket/Instagram-safe cap
VIDEO_MIN_SECONDS, VIDEO_MAX_SECONDS = 3, 90
SOURCE_MAX_SECONDS = 600
MAX_CLIPS = 10
FPS = 30


class EditError(ValueError):
    """An edit that can't be done, with a message fit to show the user."""


def default_options(preset, kind):
    """Light defaults (agreed with the owner 3 Nov 2026)."""
    return {
        "fit": "crop",            # crop | blur | solid
        "focus": [0.5, 0.5],      # crop focus point, 0..1 from top-left
        "bg_color": BRAND["colors"]["background_dark"],
        "enhance": True,
        "logo": True,
        "footer": True,
        "headline": None,         # "LINE ONE\nLINE TWO": line 1 white, line 2 purple
        "headline_position": "top",
        "subtext": None,          # small line, e.g. "Rs 2,500 | Made in Lahore"
        # video only
        "trims": None,            # [[start, end] or None per clip], seconds
        "speed": 4.0 if preset == "timelapse" else 1.0,
        "fade": True,
        "music": None,            # name of a file in the media-assets bucket (music/...)
        "music_volume": 0.8,
        "keep_audio": preset != "timelapse",
    }


def merge_options(preset, kind, opts):
    out = default_options(preset, kind)
    for k, v in (opts or {}).items():
        if k in out and v is not None:
            out[k] = v
    out["focus"] = [min(1.0, max(0.0, float(f))) for f in (out["focus"] or [0.5, 0.5])[:2]]
    if out["fit"] not in ("crop", "blur", "solid"):
        raise EditError("fit must be crop, blur or solid")
    out["speed"] = float(out["speed"] or 1.0)
    if not 0.25 <= out["speed"] <= 16:
        raise EditError("speed must be between 0.25x and 16x")
    return out


def _hex(color):
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


# ───────────────────────────── geometry ─────────────────────────────

def crop_box(src_size, ratio, focus=(0.5, 0.5)):
    """Largest box of `ratio` (w/h) inside src_size, placed so the focus
    point is as central as the edges allow."""
    sw, sh = src_size
    if sw / sh > ratio:
        h, w = sh, round(sh * ratio)
    else:
        w, h = sw, round(sw / ratio)
    x = min(max(round(focus[0] * sw - w / 2), 0), sw - w)
    y = min(max(round(focus[1] * sh - h / 2), 0), sh - h)
    return (x, y, x + w, y + h)


def output_size(content_size, target, even=False):
    """Target size, unless that means upscaling the content more than
    MAX_UPSCALE: then a smaller size with the same ratio, plus a warning."""
    tw, th = target
    factor = max(tw / content_size[0], th / content_size[1])
    if factor <= MAX_UPSCALE:
        return target, None
    scale = MAX_UPSCALE / factor
    w, h = max(320, round(tw * scale)), max(320, round(th * scale))
    if even:
        w, h = w - w % 2, h - h % 2
    return (w, h), (f"Source is low resolution ({content_size[0]}x{content_size[1]}); output is {w}x{h} "
                    f"instead of {tw}x{th} so it isn't blown up. Use a sharper original if you have one.")


# ───────────────────────────── overlays ─────────────────────────────

def _font(path, size):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def _margins(size):
    """Top/bottom safe margins. 9:16 keeps clear of Instagram's Reel UI
    (profile row at the top, caption and buttons at the bottom)."""
    w, h = size
    if h / w > 1.6:
        return round(h * 0.12), round(h * 0.17)
    return round(h * 0.035), round(h * 0.03)


def overlay_layer(size, opts, assets):
    """Transparent RGBA layer with logo, headline, subtext and footer.
    Returns (layer, placements) where placements records each element's box
    (used by tests to check nothing lands outside the safe area)."""
    w, h = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    top, bottom = _margins(size)
    white, purple = (255, 255, 255, 255), _hex(BRAND["colors"]["brand_purple"]) + (255,)
    placed, y_top = {}, top

    if opts.get("logo"):
        lw = round(w * BRAND["overlays"]["logo"]["width_pct"] / 100)
        logo_path = (assets or {}).get("logo")
        if logo_path:
            logo = Image.open(logo_path).convert("RGBA")
            logo = logo.resize((lw, max(1, round(logo.height * lw / logo.width))), Image.LANCZOS)
        else:
            # No logo uploaded yet: a plain "TD" wordmark in the brand white.
            f = _font((assets or {}).get("font"), round(lw * 0.62))
            bb = draw.textbbox((0, 0), "TD", font=f)
            logo = Image.new("RGBA", (bb[2] - bb[0] + 4, bb[3] - bb[1] + 4), (0, 0, 0, 0))
            sw = max(1, round(f.size * 0.06))
            logo = Image.new("RGBA", (logo.width + 2 * sw, logo.height + 2 * sw), (0, 0, 0, 0))
            ImageDraw.Draw(logo).text((2 + sw - bb[0], 2 + sw - bb[1]), "TD", font=f, fill=white,
                                      stroke_width=sw, stroke_fill=white)
        x = (w - logo.width) // 2
        layer.alpha_composite(logo, (x, y_top))
        placed["logo"] = (x, y_top, x + logo.width, y_top + logo.height)
        y_top += logo.height + round(h * 0.03)

    footer_top = h - bottom
    if opts.get("footer"):
        text = BRAND["overlays"]["footer"]["text"]
        f = _font(None, max(12, round(w * 0.022)))
        spacing = round(f.size * 0.45)
        widths = [draw.textlength(ch, font=f) for ch in text]
        total = sum(widths) + spacing * (len(text) - 1)
        x, y = (w - total) / 2, h - bottom - f.size
        for ch, cw in zip(text, widths):
            draw.text((x + 1, y + 1), ch, font=f, fill=(0, 0, 0, 150))  # readable on bright footage
            draw.text((x, y), ch, font=f, fill=(255, 255, 255, 235))
            x += cw + spacing
        placed["footer"] = (round((w - total) / 2), round(y), round((w + total) / 2), round(y + f.size * 1.2))
        footer_top = round(y) - round(h * 0.02)

    if opts.get("headline"):
        lines = [l.strip().upper() for l in str(opts["headline"]).splitlines() if l.strip()][:3]
        f = _fit_font(draw, lines, (assets or {}).get("font"), round(w * 0.11), round(w * 0.84))
        # The built-in fallback font is thin; thicken it until a brand font is uploaded.
        bold = 0 if (assets or {}).get("font") else max(1, round(f.size * 0.035))
        line_h = round(f.size * 1.1)
        block_h = line_h * len(lines)
        y = y_top + round(h * 0.01) if opts.get("headline_position", "top") == "top" else footer_top - block_h - round(h * 0.06)
        for i, line in enumerate(lines):
            lw_ = draw.textlength(line, font=f)
            x = (w - lw_) / 2
            draw.text((x + 3, y + i * line_h + 3), line, font=f, fill=(0, 0, 0, 110), stroke_width=bold)
            draw.text((x, y + i * line_h), line, font=f, fill=white if i == 0 else purple,
                      stroke_width=bold, stroke_fill=white if i == 0 else purple)
        placed["headline"] = (round(w * 0.08), y, round(w * 0.92), y + block_h)
        if opts.get("headline_position", "top") == "top":
            y_top = y + block_h + round(h * 0.015)
        else:
            footer_top = y - round(h * 0.015)

    if opts.get("subtext"):
        f = _fit_font(draw, [str(opts["subtext"])], None, round(w * 0.04), round(w * 0.84))
        text = str(opts["subtext"])
        tw = draw.textlength(text, font=f)
        y = footer_top - round(f.size * 1.3)
        draw.text(((w - tw) / 2 + 2, y + 2), text, font=f, fill=(0, 0, 0, 110))
        draw.text(((w - tw) / 2, y), text, font=f, fill=white)
        placed["subtext"] = (round((w - tw) / 2), y, round((w + tw) / 2), y + round(f.size * 1.2))
    return layer, placed


def _fit_font(draw, lines, font_path, start, max_width):
    size = start
    while size > 12:
        f = _font(font_path, size)
        if max(draw.textlength(l, font=f) for l in lines) <= max_width:
            return f
        size = round(size * 0.92)
    return _font(font_path, 12)


# ───────────────────────────── images ─────────────────────────────

def enhance(img):
    """Mild, never heavy: a touch of contrast and colour, light sharpening."""
    img = ImageEnhance.Contrast(img).enhance(1.06)
    img = ImageEnhance.Color(img).enhance(1.08)
    return img.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=3))


def fit_image(img, target, opts):
    """Crop (fill), or pad with a blurred copy or a solid colour, to the
    target ratio. Returns (image at final size, warning or None)."""
    tw, th = target
    ratio = tw / th
    if opts["fit"] == "crop":
        img = img.crop(crop_box(img.size, ratio, opts["focus"]))
        size, warn = output_size(img.size, target)
        return img.resize(size, Image.LANCZOS), warn
    scale = min(tw / img.width, th / img.height)
    content = (round(img.width * scale), round(img.height * scale))
    size, warn = output_size(img.size, content)
    if size != content:  # low-res source: shrink the whole canvas with it
        s = size[0] / content[0]
        tw, th = round(tw * s), round(th * s)
    fg = img.resize(size, Image.LANCZOS)
    if opts["fit"] == "blur":
        bg = ImageOps.fit(img, (max(1, tw // 8), max(1, th // 8)), Image.BILINEAR)
        bg = bg.filter(ImageFilter.GaussianBlur(3)).resize((tw, th), Image.BILINEAR)
        bg = ImageEnhance.Brightness(bg).enhance(0.6)
    else:
        bg = Image.new("RGB", (tw, th), _hex(opts["bg_color"]))
    bg.paste(fg, ((tw - fg.width) // 2, (th - fg.height) // 2))
    return bg, warn


def edit_image(data, preset, opts, assets=None):
    """bytes in -> (JPEG bytes, warnings). EXIF (including GPS location) is
    dropped from the output."""
    if preset not in IMAGE_PRESETS:
        raise EditError(f"Unknown image preset '{preset}'. Use one of: {', '.join(IMAGE_PRESETS)}.")
    opts = merge_options(preset, "image", opts)
    try:
        img = Image.open(BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        raise EditError("That image could not be read.")
    img, warn = fit_image(img, PRESETS[preset], opts)
    if opts["enhance"]:
        img = enhance(img)
    layer, _ = overlay_layer(img.size, opts, assets)
    img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    return to_jpeg(img), [warn] if warn else []


def to_jpeg(img):
    for q in (JPEG_QUALITY, 85, 80, 75, 70):
        out = BytesIO()
        img.save(out, "JPEG", quality=q, optimize=True, progressive=True)
        if out.tell() <= IG_IMAGE_MAX_BYTES:
            return out.getvalue()
    return out.getvalue()


def edit_carousel(datas, preset, opts, assets=None):
    """Every slide gets the same ratio, size and look."""
    if not 2 <= len(datas) <= 10:
        raise EditError("A carousel needs 2 to 10 images.")
    results, warnings = [], []
    for i, d in enumerate(datas):
        jpg, w = edit_image(d, preset, opts, assets)
        results.append(jpg)
        warnings += [f"Slide {i + 1}: {x}" for x in w]
    return results, warnings


# ───────────────────────────── videos ─────────────────────────────

def ffmpeg_exe():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise EditError("ffmpeg is not available on this server.")


def ffmpeg_source():
    """Which ffmpeg is in use, for /health."""
    try:
        exe = ffmpeg_exe()
    except EditError:
        return "missing"
    return "system" if exe == shutil.which("ffmpeg") else "imageio-ffmpeg"


def parse_probe(stderr):
    """Duration, display size, rotation and audio presence from `ffmpeg -i`
    output (imageio-ffmpeg ships no ffprobe)."""
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    v = re.search(r"Stream #\S+.*Video: .*?(\d{2,5})x(\d{2,5})", stderr)
    if not v:
        raise EditError("No video stream found in that file.")
    w, h = int(v.group(1)), int(v.group(2))
    r = re.search(r"rotation of (-?[\d.]+) degrees", stderr) or re.search(r"rotate\s*:\s*(-?\d+)", stderr)
    rot = abs(round(float(r.group(1)))) % 180 if r else 0
    if rot == 90:
        w, h = h, w  # ffmpeg auto-rotates, so filters see the display size
    return {"duration": dur, "width": w, "height": h, "audio": bool(re.search(r"Stream #\S+.*Audio:", stderr))}


def probe(path):
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True, text=True, errors="replace")
    info = parse_probe(p.stderr)
    if not info["duration"]:
        raise EditError("Could not read the video's length.")
    return info


def plan_video(probes, preset, opts):
    """Checks limits and works out clip windows, output size, duration and
    bitrate. Pure function (tested without ffmpeg)."""
    if preset not in VIDEO_PRESETS:
        raise EditError(f"Unknown video preset '{preset}'. Use one of: {', '.join(VIDEO_PRESETS)}.")
    if not 1 <= len(probes) <= MAX_CLIPS:
        raise EditError(f"Use 1 to {MAX_CLIPS} clips.")
    target = PRESETS[preset]
    ratio = target[0] / target[1]
    trims = opts.get("trims") or [None] * len(probes)
    if len(trims) != len(probes):
        raise EditError("Give one trim per clip (or none).")
    clips, warnings, worst = [], [], 0.0
    for i, (pr, t) in enumerate(zip(probes, trims)):
        if pr["duration"] > SOURCE_MAX_SECONDS:
            raise EditError(f"Clip {i + 1} is {pr['duration'] / 60:.0f} minutes; the limit is {SOURCE_MAX_SECONDS // 60}. Trim it on your phone first.")
        start, end = (float(t[0] or 0), float(t[1] or pr["duration"])) if t else (0.0, pr["duration"])
        end = min(end, pr["duration"])
        if end - start < 0.5:
            raise EditError(f"Clip {i + 1}: the trim leaves less than half a second.")
        clips.append({"start": start, "duration": end - start, "audio": pr["audio"]})
        if opts["fit"] == "crop":
            cw = min(pr["width"], pr["height"] * ratio)
            factor = target[0] / cw
        else:
            factor = min(target[0] / pr["width"], target[1] / pr["height"])
        worst = max(worst, factor)
    duration = sum(c["duration"] for c in clips) / opts["speed"]
    if duration < VIDEO_MIN_SECONDS:
        raise EditError(f"The result would be {duration:.1f}s; Reels need at least {VIDEO_MIN_SECONDS}s.")
    if duration > VIDEO_MAX_SECONDS:
        raise EditError(f"The result would be {duration:.0f}s; keep it under {VIDEO_MAX_SECONDS}s "
                        f"(your best Reels run 12-20s). Trim it or speed it up.")
    size = target
    if worst > MAX_UPSCALE:
        scale = MAX_UPSCALE / worst
        w, h = max(480, round(target[0] * scale)), max(480, round(target[1] * scale))
        w, h = w - w % 2, h - h % 2
        size = (w, h)
        warnings.append(f"Source video is low resolution; output is {w}x{h} instead of {target[0]}x{target[1]} so it isn't blown up.")
    total_kbps = VIDEO_MAX_BYTES * 8 / 1000 / duration
    video_kbps = int(min(8000 if size[1] >= 1080 or size[0] >= 1080 else 5000, total_kbps - 160))
    if video_kbps < 800:
        raise EditError("Too long to fit under 50 MB at decent quality. Trim it.")
    return {"clips": clips, "size": size, "duration": duration, "video_kbps": video_kbps, "warnings": warnings}


def _fit_filter(fit, w, h, focus, bg, tag=0):
    """tag keeps the blur branch's labels unique per clip when joining."""
    if fit == "crop":
        return (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h}:(iw-{w})*{focus[0]:.3f}:(ih-{h})*{focus[1]:.3f}")
    if fit == "solid":
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:({w}-iw)/2:({h}-ih)/2:color=0x{bg.lstrip('#')}")
    bw, bh = max(2, w // 8 // 2 * 2), max(2, h // 8 // 2 * 2)
    t = tag
    return (f"split[fa{t}][fb{t}];[fa{t}]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=4:1,scale={w}:{h},eq=brightness=-0.15[bg{t}];"
            f"[fb{t}]scale={w}:{h}:force_original_aspect_ratio=decrease[fg{t}];[bg{t}][fg{t}]overlay=({w}-w)/2:({h}-h)/2")


def build_video_command(sources, plan, opts, overlay_png, music_path, out_path, exe="ffmpeg"):
    """ffmpeg arguments for the whole edit: per-clip trim/fit/speed, join,
    overlay, fades, music. Streams from disk to disk (no whole-file reads in
    Python), single encode, veryfast preset, 2 threads for Render's small CPU."""
    w, h = plan["size"]
    speed, d = opts["speed"], plan["duration"]
    args = [exe, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1"]
    for src, c in zip(sources, plan["clips"]):
        args += ["-ss", f"{c['start']:.3f}", "-t", f"{c['duration']:.3f}", "-i", src]
    n = len(sources)
    args += ["-i", overlay_png]
    if music_path:
        args += ["-stream_loop", "-1", "-i", music_path]
    fil, keep = [], opts["keep_audio"] and speed == 1.0
    for i, c in enumerate(plan["clips"]):
        fil.append(f"[{i}:v]{_fit_filter(opts['fit'], w, h, opts['focus'], opts['bg_color'], i)},"
                   f"setsar=1,setpts=(PTS-STARTPTS)/{speed},fps={FPS}[v{i}]")
        if keep and c["audio"]:
            fil.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]")
        else:
            fil.append(f"anullsrc=r=44100:cl=stereo,atrim=0:{c['duration'] / speed:.3f}[a{i}]")
    fil.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    fil.append(f"[vc][{n}:v]overlay=0:0:format=auto[vo]")
    vf = "[vo]format=yuv420p"
    if opts["fade"]:
        fd = min(0.4, d / 4)
        vf = f"[vo]fade=t=in:st=0:d={fd:.2f},fade=t=out:st={d - fd:.2f}:d={fd:.2f},format=yuv420p"
    fil.append(vf + "[vf]")
    fade_a = f"afade=t=in:st=0:d=0.3,afade=t=out:st={max(0, d - 0.6):.2f}:d=0.6"
    if music_path:
        fil.append(f"[{n + 1}:a]atrim=0:{d:.3f},asetpts=PTS-STARTPTS,aresample=44100,"
                   f"aformat=channel_layouts=stereo,volume={float(opts['music_volume']):.2f}[am]")
        if keep:
            fil.append(f"[ac]volume=0.6[ao];[ao][am]amix=inputs=2:duration=first:normalize=0,{fade_a}[aout]")
        else:
            fil.append(f"[ac]anullsink;[am]{fade_a}[aout]")  # original sound dropped, music only
    else:
        fil.append(f"[ac]{fade_a}[aout]")
    kb = plan["video_kbps"]
    args += ["-filter_complex", ";".join(fil), "-map", "[vf]", "-map", "[aout]",
             "-t", f"{d:.3f}", "-r", str(FPS),
             "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-pix_fmt", "yuv420p",
             "-b:v", f"{kb}k", "-maxrate", f"{kb}k", "-bufsize", f"{kb * 2}k", "-g", str(FPS * 2),
             "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
             "-movflags", "+faststart", "-threads", "2", "-filter_threads", "1", out_path]
    return args


def write_overlay_png(size, opts, assets, path):
    layer, _ = overlay_layer(size, opts, assets)
    layer.save(path, "PNG")
    return path


def progress_from_line(line, duration):
    """ffmpeg -progress emits out_time_us=...; returns percent or None."""
    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
        try:
            secs = int(line.split("=", 1)[1]) / 1_000_000
        except ValueError:
            return None
        return max(0, min(99, int(secs / duration * 100)))
    return None
