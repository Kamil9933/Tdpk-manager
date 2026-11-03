# TDPK marketing agent: scope

Note: this file was created on 3 Nov 2026. No earlier copy was found in the
repo or its git history, so it only records phases visible in the code plus
the Phase 4 storage work. Add the original phase plans here if they live
somewhere else.

## Phases so far (from the code)
- **Phase 1**: Meta client in the backend (existing ads, Facebook Page posts,
  Instagram posts, comments, insights). Every write is preview-then-confirm.
- **Phase 2**: new ad campaigns (Campaign, Ad Set, Creative, Ad in one call),
  always created PAUSED and always behind explicit approval.
- **Phase 3**: in-app Manager chat (Tier 1 rules, Gemini, Claude) using the
  same tools as the MCP connector and Telegram.
- **Phase 4**: media for social posts. **Storage and editing shipped
  (below). AI-generated visuals still open.**

## Phase 4, storage half (shipped 3 Nov 2026)
Instagram and Facebook fetch media by public URL, so TDPK now hosts the files.

- **Bucket**: public Supabase Storage bucket `social-media`
  (`social_media_schema.sql`). 50 MB limit; JPEG, PNG, MP4, MOV. Anyone can
  read; only the backend (service role) can write or delete.
- **Backend** (`core/supabase_client.py`): `upload_media`, `list_media`,
  `get_media`, `delete_media`, `purge_old_media`. Unique names
  (timestamp + random suffix + cleaned filename). PNG is converted to JPEG
  (Instagram's feed needs JPEG); videos are stored untouched.
- **API** (`main.py`, dashboard login required): `POST /api/media/upload`,
  `GET /api/media`, `DELETE /api/media/{path}`, `POST /api/media/publish`
  (preview unless `confirm: true`).
- **Dashboard**: Ads & Social > Media. Drag-and-drop upload, thumbnails and
  video previews, Copy URL, Delete. "Publish to Instagram" / "Post to
  Facebook" open a preview (media, caption, account, delete-after option)
  and post only after "Confirm & post". Videos go out as Reels on Instagram
  and as Page videos on Facebook. The Auto social publishing toggle is shown
  in the preview but never skips it.
- **Agent tools** (`core/mcp_server.py`): `list_media`, `get_media_url`;
  `publish_instagram_image`, `publish_instagram_reel` and `post_to_facebook`
  accept a `media_id` or a URL and still return a preview without
  `confirm=true`. Reels wait for Instagram to finish processing (up to
  5 minutes) and fail with a clear error instead of publishing early.
- **Chat**: `media` lists uploaded files without any AI call; listed in `help`.
- **Cleanup**: the cron tick deletes files older than `MEDIA_RETENTION_DAYS`
  (default 7). The preview also offers "delete the file after posting".

## Phase 4 add-on: editing (shipped 3 Nov 2026)
Raw photos and clips become post-ready files inside TDPK. Deterministic code
only (`core/media_edit.py`), no AI. Every edit makes a NEW file in the
`social-media` bucket linked to its source; originals are never overwritten.
Publishing an edit uses the same preview + confirm as any other post.

- **Brand style** (`core/brand_style.json`), studied from the last 30 posts and
  confirmed by the owner: 4:5 1080x1350 feed posts, 9:16 1080x1920 Reels,
  brand purple #8E57DE, white TD mark top centre, "www.thirddimension.pk"
  footer, brush-font headline (line 1 white, line 2 purple), short hook +
  emoji captions, Reels 12-20 s.
  Finding worth remembering: the branded posts (Jun-Jul 2026) reached 205-591
  accounts each, while raw phone Reels with a question or real-life hook
  (Oct 2025-May 2026) reached 756-2,057. So defaults are light: small logo,
  footer, mild enhance, headline only when asked.
- **Images (Pillow)**: presets feed, square, story, facebook, landscape;
  crop with a focus point, or blurred / dark padding; mild enhance; logo,
  headline, small text and footer overlays inside Reel-safe margins; JPEG
  q90 under 8 MB; GPS/EXIF removed. Carousel: 2-10 photos normalised to one
  size and look.
- **Videos (ffmpeg)**: presets reel, timelapse (4x), story, facebook,
  square, landscape; trim, join up to 10 clips, speed 0.25-16x, crop/blur/pad
  to ratio, same overlays as photos (drawn once as a PNG), fades, optional
  music from the assets bucket (replace or mix). Output H.264 high/yuv420p,
  30 fps, AAC 128k, faststart, bitrate chosen to stay under 48 MB, 3-90 s.
  Low-res sources are output smaller with a warning instead of upscaled
  past 1.5x.
- **ffmpeg choice**: `imageio-ffmpeg` (pip package with a static ffmpeg 7.1),
  because Render's free tier has no shell to confirm a system ffmpeg and a
  Docker deploy isn't needed for this. A system ffmpeg on PATH wins if
  present; `/health` reports which one is used.
- **Infra**: jobs run in the server's event loop, one at a time
  (`-preset veryfast`, 2 threads, disk-to-disk streaming) to fit 512 MB.
  `media_jobs` table tracks queued/running/done/failed with progress.
  Jobs cut off by a restart are marked failed at startup. Render's free
  instance sleeps after ~15 min idle; the dashboard's 2 s polling keeps it
  awake during a render.
- **Assets**: private `media-assets` bucket holds `logo.png` (transparent
  PNG required), `font.ttf/.otf` and `music/...`. Upload on Media > Brand
  assets. Without them a plain "TD" mark and Pillow's built-in font are used.
- **API**: `POST /api/media/{path}/edit` (options.join for more clips),
  `POST /api/media/carousel`, `GET /api/media/jobs[/{id}]`,
  `GET/POST/DELETE /api/media/assets`. `/api/media/publish` takes `paths`
  for Instagram carousels and Facebook multi-photo posts.
- **Dashboard**: Edit on every item (presets, fit, focus click, logo/footer/
  enhance, headline, small text, trim, speed, music, join clips), live
  progress, before/after side by side, Publish buttons on the result.
  "Carousel from selected" for 2-10 ticked photos.
- **Agent**: `edit_image`, `edit_video`, `make_carousel`, `get_media_job`,
  `list_media_jobs`, `list_media_assets`, `publish_instagram_carousel`,
  `draft_caption` (deterministic, in the account's style, never posts).
  Edits are Tier 3 (no confirm); publishing stays Tier 2. Chat: `edit status`.
- **Fixed on the way**: Meta list responses carried `paging.next` URLs with
  the access token inside, which reached chat, Telegram and Gemini. They are
  now stripped.

## Still open
- **AI-generated visuals**: new backgrounds, product cut-outs, generated
  scenes or voice-overs. Today's editing only reworks real footage.
- Instagram Stories publishing (files can be made 9:16, but the API call
  isn't wired) and scheduling (Instagram has no scheduling API).
- Beat-synced cuts and automatic highlight picking from long clips.
- Scheduling uploaded videos on Facebook (photos and text posts can already
  be scheduled; Instagram has no scheduling API).
- Carousels (several images in one Instagram post) and Stories.
- Autonomous posting: the `social_publish` toggle exists, but nothing posts
  on its own yet, and every path still requires a confirm.
