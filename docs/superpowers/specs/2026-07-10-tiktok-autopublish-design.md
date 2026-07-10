# TikTok autopublish — design

## Context

Catapult-Bot generates YouTube Shorts (`orchestrator.py` + `subagents/yt_*.py`) and publishes them after admin approval (`subagents/yt_publisher.py:handle_video_approval`). The user wants the same approved video to also post to TikTok, without a separate manual step. Instagram and Twitter/X are planned follow-ups, out of scope here.

## Constraint: TikTok's own API

TikTok's official Content Posting API requires a two-stage approval to post publicly: standard developer app approval, then a separate audit to lift the `SELF_ONLY` visibility restriction. Until audited, every post made through the raw API is visible only to the poster's own account — not actually published. The audit can take days to weeks and isn't guaranteed.

Decision: route through **Upload-Post** (upload-post.com), a third-party posting API that has already cleared this audit. Free tier for testing; $16/month unlocks unlimited posts across up to 5 connected profiles (covers the later Instagram/Twitter work too, on the same subscription).

## Architecture

New subagent `subagents/tiktok_publisher.py`:

```python
async def upload_to_tiktok(video_path: str, title: str) -> str | None:
    """POSTs video_path to Upload-Post's /api/upload with platform[]=tiktok.
    Returns the TikTok post URL, or None on failure (never raises)."""
```

- Auth: `Authorization: Apikey {UPLOAD_POST_API_KEY}` header.
- Body: multipart form — `user={UPLOAD_POST_PROFILE}`, `platform[]=tiktok`, `video=<file>`, `title=<title>`.
- Same title/description already generated for YouTube is reused verbatim — no separate TikTok-specific caption generation in this pass.

## Data flow

In `handle_video_approval` (`subagents/yt_publisher.py`), inside the `action == "vapprove"` branch, after `upload_to_youtube` succeeds and before the local video file is removed:

1. Call `upload_to_tiktok(video["video_path"], video["title"])`.
2. Build one combined status message for the admin instead of the current YouTube-only one:
   - Both succeeded: `✅ YouTube: {yt_link}\n✅ TikTok: {tt_link}`
   - TikTok failed: `✅ YouTube: {yt_link}\n⚠️ TikTok не удался — /retry_tiktok`
3. Delete `video["video_path"]` only if TikTok succeeded (or was never attempted, e.g. no key configured). If TikTok failed, keep the video entry (with its file) in a new `tiktok_retry_pending: dict` (mirrors the existing `approved_videos` retry-store pattern used for YouTube failures), so nothing needs to be regenerated.

## Retry path

New admin command `/retry_tiktok`, mirroring the existing `/retry_videos` → `yt_publisher.retry_upload` pattern:

- `parser.py`: `CommandHandler("retry_tiktok", cmd_retry_tiktok)`, iterates `tiktok_retry_pending` and calls a new `yt_publisher.retry_tiktok_upload(video_id)` for each.
- `retry_tiktok_upload(video_id)`: re-attempts only `upload_to_tiktok` for that entry (YouTube is not touched — it already succeeded). On success, removes the entry from `tiktok_retry_pending`, deletes the local video file, and confirms in the admin chat.

## Error handling

- YouTube upload success is never blocked or rolled back by a TikTok failure — this was an explicit decision (the two platforms are independent once YouTube succeeds).
- `upload_to_tiktok` catches its own exceptions and returns `None` — callers never need to handle raised exceptions from it, consistent with the rest of the `yt_*` subagents (`generate_image`, `generate_voiceover`, `render_video` all follow this same "return `None` on failure" convention).
- If `UPLOAD_POST_API_KEY` is unset, `upload_to_tiktok` logs a warning and returns `None` immediately (same pattern as `ELEVENLABS_API_KEY` / `JSON2VIDEO_API_KEY` checks elsewhere) — TikTok posting is silently skipped rather than blocking YouTube.

## Configuration

Two new environment variables, set on Railway for the `Catapult-Bot` service (not needed on `web`, since TikTok upload happens directly from Catapult-Bot using the local video file, same as the existing YouTube upload):

- `UPLOAD_POST_API_KEY` — from the Upload-Post dashboard.
- `UPLOAD_POST_PROFILE` — the profile username created when connecting the TikTok account via Upload-Post's OAuth flow in their dashboard.

## Out of scope for this pass

- Instagram and Twitter/X posting (explicitly deferred by the user; Upload-Post's multi-profile pricing already anticipates this).
- TikTok-specific caption/hashtag generation (reusing the YouTube title/description for now).
- Scheduling (`scheduled_date`) or comment automation (`first_comment`) — Upload-Post supports these but nothing in the current pipeline needs them yet.
