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
- **Phase 4**: media for social posts. **Storage half shipped (below).
  Generation half still open.**

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

## Still open
- **Generating images and videos** (the other half of Phase 4): creating or
  editing product photos and Reels inside TDPK. Today the files are edited
  elsewhere and uploaded.
- Scheduling uploaded videos on Facebook (photos and text posts can already
  be scheduled; Instagram has no scheduling API).
- Carousels (several images in one Instagram post) and Stories.
- Autonomous posting: the `social_publish` toggle exists, but nothing posts
  on its own yet, and every path still requires a confirm.
