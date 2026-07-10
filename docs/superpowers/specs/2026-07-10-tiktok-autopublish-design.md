# TikTok autopublish — design

## Context

Catapult-Bot generates YouTube Shorts (`orchestrator.py` + `subagents/yt_*.py`) and publishes them after admin approval (`subagents/yt_publisher.py:handle_video_approval`). The user wants the same approved video to also post to TikTok, without a separate manual step. Instagram and Twitter/X are planned follow-ups, out of scope here.

## Constraint: TikTok's own API

TikTok's official Content Posting API requires a two-stage approval to post publicly: standard developer app approval, then a separate audit to lift the `SELF_ONLY` visibility restriction. Until audited, every post made through the raw API is visible only to the poster's own account — not actually published. The audit can take days to weeks and isn't guaranteed.

Decision: route through **Buffer**'s API (already an audited TikTok partner — no separate audit needed on our side). Buffer's free plan includes TikTok as one of up to 3 connected channels, and API access (100 requests/24h, 3,000/30 days) is included on every plan including Free — no cost. (Upload-Post was considered first but gates TikTok behind a paid plan; Buffer does not.)

## Constraint: video must be a public URL

Buffer's `createPost` mutation takes `assets: [{video: {url}}]` — a public HTTPS URL, not a file upload. Confirmed live: a URL Buffer's server can't fetch fails fast with a clear error (`"Video URL is not accessible: HTTP 403 Forbidden..."`), so this is validated server-side before anything reaches TikTok.

Catapult-Bot's rendered video currently only exists on its own local disk (`/data/videos`, `VIDEOS_DIR` in `yt_publisher.py`) — it's never been exposed over HTTP (YouTube upload and Telegram's `send_video` both read the local file directly). This is the same problem Task 2 of the original `youtube-shorts-pipeline` plan solved for photos/audio: `web`'s `/media/{kind}/{filename}` endpoint (`server.py`) plus `subagents/media_push.py` (pushes a locally-generated file to `web` over HTTP, since `Catapult-Bot` and `web` have separate Railway volumes — see `project_catapult_bot` memory). This design extends both to cover `"videos"`.

## Architecture

New subagent `subagents/tiktok_publisher.py`:

```python
async def upload_to_tiktok(video_path: str, title: str) -> str | None:
    """Publishes video_path to TikTok via Buffer's GraphQL API. Pushes the
    file to web's /media endpoint first to get a public URL, then calls
    Buffer's createPost + polls until the post is sent. Returns the TikTok
    post URL, or None on failure (never raises)."""
```

### Confirmed Buffer API shape (live-tested 2026-07-10 against the real `crypto_ai_forex` TikTok channel via Buffer)

- Endpoint: `https://api.buffer.com` (GraphQL), `Authorization: Bearer {BUFFER_API_KEY}`.
- Create the post:
  ```graphql
  mutation($input: CreatePostInput!) {
    createPost(input: $input) {
      ... on PostActionSuccess { post { id status } }
      ... on InvalidInputError { message }
      ... on UnauthorizedError { message }
      ... on UnexpectedError { message }
      ... on LimitReachedError { message }
      ... on NotFoundError { message }
      ... on RestProxyError { message }
    }
  }
  ```
  ```json
  {
    "input": {
      "channelId": "<BUFFER_TIKTOK_CHANNEL_ID>",
      "text": "<title>",
      "mode": "shareNow",
      "schedulingType": "automatic",
      "assets": [{"video": {"url": "<public video URL>"}}]
    }
  }
  ```
  Returns immediately with `post.status == "sending"` — publishing to TikTok happens asynchronously.
- Poll for completion:
  ```graphql
  query($id: PostId!) {
    post(input: {id: $id}) { id status sentAt externalLink }
  }
  ```
  `status` is one of `draft | needs_approval | scheduled | sending | sent | error`. Observed in the live test: `sending` → `sent` within ~5s, with `externalLink` populated as the real TikTok post URL (e.g. `https://tiktok.com/@crypto_ai_forex/video/...`). Poll with a bounded loop + short sleep (same shape as `yt_render.py`'s JSON2Video polling loop), treating `sent` as success (return `externalLink`) and `error` as failure (return `None`).
- Same title already generated for YouTube is reused verbatim as the TikTok `text` — no separate TikTok-specific caption generation in this pass.

## Data flow

In `handle_video_approval` (`subagents/yt_publisher.py`), inside the `action == "vapprove"` branch, after `upload_to_youtube` succeeds and before the local video file is removed:

1. Call `upload_to_tiktok(video["video_path"], video["title"])` — internally, this first pushes the video to `web` (extending the existing `media_push.py` pattern to a `"videos"` kind) to obtain the public URL Buffer needs.
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
- If `BUFFER_API_KEY` or `BUFFER_TIKTOK_CHANNEL_ID` is unset, `upload_to_tiktok` logs a warning and returns `None` immediately (same pattern as `ELEVENLABS_API_KEY` / `JSON2VIDEO_API_KEY` checks elsewhere) — TikTok posting is silently skipped rather than blocking YouTube.
- The polling loop is bounded (mirrors `render_video`'s ~10-minute bound) — if Buffer never resolves out of `sending`, treat it as a failure and return `None` rather than looping forever.

## Configuration

Three new environment variables, set on Railway for **both** `Catapult-Bot` and `web` (the video-URL push needs `MEDIA_SERVE_TOKEN` on `web`'s side already; `BUFFER_API_KEY`/`BUFFER_TIKTOK_CHANNEL_ID` are only read by `Catapult-Bot`, which is where `upload_to_tiktok` runs):

- `BUFFER_API_KEY` — from Buffer's account API settings (`publish.buffer.com/account/apps` or similar).
- `BUFFER_TIKTOK_CHANNEL_ID` — confirmed live as `6a511c9e4048344628906e84` for the `crypto_ai_forex` TikTok channel (fetched via Buffer's `channels(input: {organizationId})` query).

## Out of scope for this pass

- Instagram and Twitter/X posting (explicitly deferred by the user — Buffer supports both, same account, when that work starts).
- TikTok-specific caption/hashtag generation (reusing the YouTube title/description for now).
- Buffer's `dueAt`/scheduled posting or tagging — nothing in the current pipeline needs them yet, always `mode: shareNow`.
