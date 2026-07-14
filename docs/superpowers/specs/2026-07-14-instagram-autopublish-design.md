# Instagram autopublish — design

## Context

Catapult-Bot already publishes photo posts to Telegram 7×/day (`subagents/tg_publisher.py:auto_publish`) and YouTube Shorts + TikTok after admin approval (`subagents/yt_publisher.py:_finish_publish`, added in the TikTok autopublish pass — see `docs/superpowers/specs/2026-07-10-tiktok-autopublish-design.md`). The user wants the same two approved pieces of content to also post to Instagram — feed photos duplicating the Telegram post, Reels duplicating the video pipeline — from the same single approval action, with Instagram-specific caption/hashtag generation rather than reusing the source text verbatim (unlike TikTok, which reuses the YouTube title/description as-is).

## Confirmed Buffer setup (live-tested 2026-07-14)

The Instagram account `@crypto.ai.forex` is Business/Creator and was connected to the same Buffer organization already used for TikTok (`crypto_ai_forex`), via Buffer's newer direct Instagram Login flow — no Facebook Page linking was required this time, unlike what the older Facebook-mediated flow would have needed. Confirmed via `channels(input: {organizationId})`:

```json
{"data":{"channels":[
  {"id":"6a511c9e4048344628906e84","service":"tiktok"},
  {"id":"6a5600c480cc80cdcab03633","service":"instagram"}
]}}
```

`BUFFER_INSTAGRAM_CHANNEL_ID=6a5600c480cc80cdcab03633` is already set on Railway (`Catapult-Bot` service only, same as `BUFFER_TIKTOK_CHANNEL_ID` — this is the only service that runs the publish code), deploy skipped since the code doesn't exist yet.

Buffer's `createPost` mutation shape for images is assumed to mirror the video shape confirmed for TikTok (`assets: [{"image": {"url": ...}}]` instead of `{"video": {"url": ...}}`) — this is Buffer's documented convention but has NOT been live-tested yet (TikTok is video-only, so the image asset shape was never exercised). First implementation pass must confirm this against a real photo post before considering Task complete, same as the TikTok pass confirmed its shape live.

## Architecture

**`subagents/buffer_publisher.py`** (new) — extracted from `tiktok_publisher.py`'s Buffer logic, parametrized:

```python
async def publish_to_buffer(channel_id: str, caption: str, media_url: str, media_type: str) -> str | None:
    """media_type: "video" or "image". Publishes via Buffer's createPost + polls
    until sent. Returns the post's external link, or None on failure (never raises)."""
```

`tiktok_publisher.py::upload_to_tiktok` becomes a thin wrapper (`return await publish_to_buffer(BUFFER_TIKTOK_CHANNEL_ID, caption, video_url, "video")`) — no change to its external interface or call sites.

**`subagents/instagram_caption.py`** (new) — one Claude-backed function:

```python
async def generate_instagram_caption(source_text: str, category: str, content_type: str) -> dict:
    """content_type: "photo" or "reel". Returns {"caption": str, "hashtags": list[str]}.
    Reel captions are shorter/punchier; photo captions can run longer. Hashtags mix
    broad (#crypto, #bitcoin), niche, and branded (#catapulttrade) tags, capped at
    Instagram's practical limit (~15-20 effective tags, not the hard cap of 30)."""
```

Final post text is `f"{caption}\n\n{' '.join(hashtags)}"`.

**`subagents/instagram_publisher.py`** (new):

```python
async def upload_photo_to_instagram(photo_path: str, source_text: str, category: str) -> str | None
async def upload_reel_to_instagram(video_path: str, source_text: str, category: str) -> str | None
```

Both: skip with a warning (return `None`) if `BUFFER_API_KEY`/`BUFFER_INSTAGRAM_CHANNEL_ID` unset; otherwise generate the caption, then call `publish_to_buffer` with a media URL — obtained differently per type:
- **Photo:** `generate_image` (`image_generator.py:52`) already calls `push_media("photos", ...)` at generation time, for every generated image, well before `auto_publish` ever runs — by the time Instagram publish is attempted, the file is already on `web`'s side. `upload_photo_to_instagram` builds the URL directly (same `_media_url("photos", photo_path)` helper `tiktok_publisher.py` already has), no redundant push.
- **Reel:** the rendered video is only ever pushed on-demand, inside `tiktok_publisher.py::upload_to_tiktok` today — there's no earlier eager push to piggyback on. `upload_reel_to_instagram` must call `push_media("videos", video_path)` itself, same as `upload_to_tiktok` does.

## Data flow — photo posts

**Gotcha found while reading the current code:** `tg_publisher.py:auto_publish` deletes `post["photo_path"]` immediately after `send_photo` succeeds (lines ~715 and ~742, both the poll and regular branches) — before the function does anything else. Calling Instagram publish *after* that point would hand `upload_photo_to_instagram` a path that no longer exists. The Instagram call must happen **before** the `os.remove`, not after.

Revised order in both branches of `auto_publish`:
1. `send_photo` to Telegram (unchanged).
2. Immediately after, call `instagram_publisher.upload_photo_to_instagram(post["photo_path"], post["text"], category)` — before the `os.remove`.
3. If it succeeds: remove `photo_path` as today.
4. If it fails (returns `None`) or isn't configured: still remove `photo_path` as today (photos aren't worth keeping around for retry — a fresh one will generate at the next slot naturally, unlike video which is expensive to regenerate) — just log a warning and notify the admin. **No retry-pending store for photos** (see Error handling below for why this differs from the video/Reels path).
5. Telegram publish is never blocked or rolled back by an Instagram failure (same independence rule as YouTube/TikTok).

## Data flow — Reels

Extends `_finish_publish` in `yt_publisher.py`, alongside the existing YouTube + TikTok logic, as its own independent branch (not nested inside the TikTok compliance-check branch — Instagram does not need TikTok's `check_tiktok_compliance` gate; Instagram's community guidelines don't carry the same blanket financial-content restriction TikTok does, so no fallback-script step is needed here):

```python
instagram_url = await upload_reel_to_instagram(video["video_path"], _tiktok_caption(video), video["category"])
if instagram_url:
    status_lines.append(f"✅ Instagram: {instagram_url}")
else:
    status_lines.append("⚠️ Instagram не удался — /retry_instagram")
    instagram_retry_pending[video_id] = video
    save_instagram_retry_pending()
```

Placed after the existing TikTok if/else block, run unconditionally in both branches (TikTok-blocked and TikTok-clean) — Instagram's own attempt doesn't depend on TikTok's compliance outcome. (Reusing `_tiktok_caption(video)`'s raw title+description as the *source text* fed into `generate_instagram_caption` — not posted verbatim; Instagram gets its own generated caption/hashtags from that source, unlike TikTok which posts the raw text directly.)

`instagram_retry_pending: dict` mirrors `tiktok_retry_pending` exactly (own JSON file `/data/instagram_retry_pending.json`, `save_instagram_retry_pending()`/load-on-startup, same shape).

### File-lifecycle fix (found during self-review, not in the original TikTok code)

Today, `_finish_publish` deletes `video["video_path"]` as soon as TikTok's own attempt is resolved (success, or a compliance-fallback attempt that doesn't get retried) — this is safe *today* because TikTok is the only platform that can leave a pending retry pointing at that file. Adding Instagram as a second independent retry-capable consumer of the same file breaks that assumption: if TikTok fails (kept for `tiktok_retry_pending`) *and* Instagram fails (kept for `instagram_retry_pending`), whichever retries successfully first must NOT delete the file out from under the other's still-pending retry.

Fix, touching both the new Instagram code and the existing TikTok retry code:
- In `_finish_publish`'s TikTok-clean (`else`) branch: its existing `os.remove` only fires today when `tiktok_url` is truthy (success) — replace that single condition with `if tiktok_url and video_id not in instagram_retry_pending: os.remove(...)`, so a TikTok success doesn't delete a file Instagram still needs to retry. The compliance-blocked (`if block_reason`) branch's own unconditional delete is untouched — it never creates a TikTok retry entry, but it still needs the same `and video_id not in instagram_retry_pending` guard added, for the same reason.
- In `retry_tiktok_upload` (existing function, needs a small edit): after its own success, delete the file only `if video_id not in instagram_retry_pending`.
- In the new `retry_instagram_upload`: after its own success, delete the file only `if video_id not in tiktok_retry_pending`.

## Retry path

New admin command `/retry_instagram` (`parser.py`), mirroring `/retry_tiktok`:
- Iterates `instagram_retry_pending` (Reels only — see above for why photos have no retry store), calls new `yt_publisher.retry_instagram_upload(video_id)` per entry.
- `retry_instagram_upload(video_id)`: re-attempts only `upload_reel_to_instagram` for that entry. On success, pops from `instagram_retry_pending`, deletes the local video file *only if TikTok has no pending retry for the same `video_id`* (see file-lifecycle fix above), confirms in the admin chat. On repeat failure, leaves the entry in place for a later retry.

## Error handling

- `upload_photo_to_instagram`/`upload_reel_to_instagram` never raise — same "return `None` on failure" convention as every other `yt_*`/`tiktok_*` subagent.
- Missing `BUFFER_API_KEY`/`BUFFER_INSTAGRAM_CHANNEL_ID` → warning + `None`, silent skip, never blocks Telegram/YouTube/TikTok.
- `publish_to_buffer`'s polling loop is bounded the same way `tiktok_publisher.py`'s already is (~2 minutes) — no independent timeout logic to write.
- Photos get no retry-pending store (see Data flow above) — video/Reels do, matching the existing TikTok precedent, because regenerating a photo is cheap (next scheduled slot) while regenerating a video (script + voiceover + render) is not.

## Configuration

One new environment variable, `Catapult-Bot` service only (same rationale as `BUFFER_TIKTOK_CHANNEL_ID` — this is the only service that runs publish code):
- `BUFFER_INSTAGRAM_CHANNEL_ID` — confirmed live as `6a5600c480cc80cdcab03633` (already set on Railway, see above).

No new variables needed for `web` — it already has `MEDIA_SERVE_TOKEN` from the TikTok/video-push work.

## Out of scope for this pass

- Instagram Stories (only feed photos + Reels, per explicit scope decision).
- Twitter/X posting (still deferred, per the original TikTok spec).
- Buffer's `dueAt`/scheduled posting — always `mode: shareNow`, same as TikTok.
- Verifying the image `assets` shape against Buffer's live API — flagged above as a first-implementation-step confirmation, not assumed safe.
