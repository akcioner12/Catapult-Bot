# TikTok Autopublish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After an admin approves a generated Short, publish it to TikTok automatically alongside the existing YouTube upload, per `docs/superpowers/specs/2026-07-10-tiktok-autopublish-design.md`.

**Architecture:** New `subagents/tiktok_publisher.py` module (same shape as `yt_voice.py`/`image_generator.py`: module-level `os.getenv` config, one async function, `None`-on-failure). It first exposes the local video over HTTP by extending `web`'s existing `/media` endpoint (`server.py`) to also serve a `"videos"` kind, then calls Buffer's GraphQL API (`createPost` + poll `post`) to publish to TikTok. Wired into the existing approval flow in `subagents/yt_publisher.py` via one shared helper (`_finish_publish`) so both the first-time approval path and the existing YouTube-retry path get TikTok publishing for free. A new `tiktok_retry_pending` dict (JSON-persisted, mirroring the existing `approved_videos` store) tracks videos where YouTube succeeded but TikTok didn't, backing a new `/retry_tiktok` admin command.

**Tech Stack:** Python 3.11, httpx (already a dependency) — no new packages. Buffer's GraphQL API (`api.buffer.com`) via plain HTTP, matching the project's existing style of calling ElevenLabs/JSON2Video without SDKs.

## Global Constraints

- Same Railway service (`Catapult-Bot`), same repo, same admin Telegram bot — no new service.
- TikTok posting must never block or roll back a successful YouTube upload — this was an explicit user decision during brainstorming.
- `upload_to_tiktok` must never raise — it returns `None` on any failure, exactly like `generate_image`, `generate_voiceover`, `render_video`, `upload_to_youtube` in this codebase.
- No automated pytest suite in this repo (confirmed precedent: `docs/superpowers/plans/2026-07-05-youtube-shorts-pipeline.md`, Task 1). Every task below verifies manually — a real API call, an admin Telegram command, or Railway log inspection — consistent with that precedent.
- Every commit in this plan goes straight to `main` and is pushed immediately (user explicitly confirmed), matching how every other fix landed the night of 2026-07-09/10 — this repo currently has no long-lived feature branch for this work, and Railway auto-deploys `main`.
- After every push that touches `Catapult-Bot`, check whether the legacy `exciting-patience` Railway service (id `e1d6be12-b4b2-45f4-a9cc-f650a8971cc3`) woke up and is fighting the bot for `getUpdates` — stop it with `railway down -y --service e1d6be12-b4b2-45f4-a9cc-f650a8971cc3` if so (see `project_catapult_bot` memory for the full recurring-incident writeup).
- The Buffer API shape below (mutation, input fields, enum values, response fields) was confirmed live on 2026-07-10 against the real `crypto_ai_forex` TikTok channel — it is not a guess, use it verbatim.

---

### Task 1: Upload-Post account setup — SUPERSEDED, already complete

~~Original Task 1 (Upload-Post signup)~~ — superseded: Upload-Post gates TikTok behind a paid plan, so the design switched to Buffer (free tier includes TikTok). This was done directly by the controller (not a subagent, per the plan's own precedent for manual account-setup steps):

- Buffer account created, TikTok channel `crypto_ai_forex` connected.
- API key generated: stored as `BUFFER_API_KEY`.
- TikTok channel ID confirmed live via `channels(input: {organizationId})` query: `6a511c9e4048344628906e84` — stored as `BUFFER_TIKTOK_CHANNEL_ID`.
- Full `createPost` → poll `post` round trip confirmed working against the real channel (see design doc for the exact request/response captured).

Both env vars are already set on Railway for `Catapult-Bot` (`--skip-deploys`, applied by whichever task's deploy runs next). Task 2 reads them via `os.getenv` — no further setup needed.

---

### Task 2: `subagents/tiktok_publisher.py` + expose video over HTTP

**Files:**
- Create: `subagents/tiktok_publisher.py`
- Modify: `server.py` (one line — add `"videos"` to `MEDIA_DIRS`)

**Interfaces:**
- Consumes: `push_media(kind: str, local_path: str) -> bool` from `subagents/media_push.py` (existing — pushes a local file to `web`'s `/media` endpoint).
- Produces: `async def upload_to_tiktok(video_path: str, title: str) -> str | None` — the exact signature Task 3 wires into `yt_publisher.py`. Returns the TikTok post URL on success, `None` on any failure (missing config, missing file, push failure, HTTP error, Buffer error, poll timeout) — never raises.

- [ ] **Step 1: Add `"videos"` to `server.py`'s `MEDIA_DIRS`**

In `server.py`, find this line (near `VIDEOS_DIR = "/data/videos"`):

```python
MEDIA_DIRS = {"photos": "/data/photos", "audio": "/data/audio"}
```

Replace with:

```python
MEDIA_DIRS = {"photos": "/data/photos", "audio": "/data/audio", "videos": "/data/videos"}
```

This is the only change needed on the `web` side — the existing `GET /media/{kind}/{filename}` and `POST /media/{kind}/{filename}` handlers (added for photos/audio) already work for any key present in `MEDIA_DIRS`.

- [ ] **Step 2: Write `subagents/tiktok_publisher.py`**

```python
"""
Sub-agent: публикация видео в TikTok через Buffer (уже прошедший аудит
TikTok Content Posting API — без него загрузка через официальный API TikTok
была бы видна только самому аккаунту, не подписчикам).
"""
import asyncio
import logging
import os

import httpx

from subagents.media_push import push_media, BACKEND_URL, MEDIA_SERVE_TOKEN

logger = logging.getLogger(__name__)

BUFFER_API_KEY = os.getenv("BUFFER_API_KEY", "")
BUFFER_TIKTOK_CHANNEL_ID = os.getenv("BUFFER_TIKTOK_CHANNEL_ID", "")
BUFFER_URL = "https://api.buffer.com"

CREATE_POST_MUTATION = """
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
"""

POST_STATUS_QUERY = """
query($id: PostId!) {
  post(input: {id: $id}) { id status sentAt externalLink }
}
"""


def _media_url(kind: str, local_path: str) -> str:
    filename = os.path.basename(local_path)
    return f"{BACKEND_URL}/media/{kind}/{filename}?token={MEDIA_SERVE_TOKEN}"


async def upload_to_tiktok(video_path: str, title: str) -> str | None:
    """Публикует video_path в TikTok через Buffer. Возвращает ссылку на пост или None."""
    if not BUFFER_API_KEY or not BUFFER_TIKTOK_CHANNEL_ID:
        logger.warning("BUFFER_API_KEY/BUFFER_TIKTOK_CHANNEL_ID не заданы — пропускаем публикацию в TikTok")
        return None
    if not os.path.exists(video_path):
        logger.error(f"upload_to_tiktok: файл не найден {video_path}")
        return None
    if not await push_media("videos", video_path):
        logger.error("upload_to_tiktok: не удалось выложить видео на web")
        return None

    video_url = _media_url("videos", video_path)
    headers = {"Authorization": f"Bearer {BUFFER_API_KEY}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                BUFFER_URL,
                headers=headers,
                json={
                    "query": CREATE_POST_MUTATION,
                    "variables": {
                        "input": {
                            "channelId": BUFFER_TIKTOK_CHANNEL_ID,
                            "text": title[:150],
                            "mode": "shareNow",
                            "schedulingType": "automatic",
                            "assets": [{"video": {"url": video_url}}],
                        }
                    },
                },
            )
            data = resp.json()
            result = data.get("data", {}).get("createPost", {})
            if "message" in result:
                logger.error(f"Buffer createPost error: {result['message']}")
                return None
            post_id = result.get("post", {}).get("id")
            if not post_id:
                logger.error(f"Buffer createPost: неожиданный ответ: {data}")
                return None

            for _ in range(24):  # до ~2 минут ожидания публикации
                await asyncio.sleep(5)
                status_resp = await client.post(
                    BUFFER_URL,
                    headers=headers,
                    json={"query": POST_STATUS_QUERY, "variables": {"id": post_id}},
                )
                post = status_resp.json().get("data", {}).get("post", {})
                if post.get("status") == "sent":
                    logger.info(f"✅ Опубликовано в TikTok: {post['externalLink']}")
                    return post["externalLink"]
                if post.get("status") == "error":
                    logger.error(f"Buffer: публикация в TikTok завершилась ошибкой: {post}")
                    return None

            logger.error("Buffer: публикация в TikTok не завершилась за отведённое время")
            return None
    except Exception as e:
        logger.error(f"upload_to_tiktok error: {e}")
        return None
```

- [ ] **Step 3: Verify against the real API (not a mock — this project has no test suite, so this is the test)**

```bash
cd "/c/Users/Андрей/catapult-bot-git"
python -c "
import asyncio, os
os.environ['BUFFER_API_KEY'] = 'your-key-here'
os.environ['BUFFER_TIKTOK_CHANNEL_ID'] = '6a511c9e4048344628906e84'
os.environ['MEDIA_SERVE_TOKEN'] = 'your-media-serve-token-here'
os.environ['BACKEND_URL'] = 'https://web-production-9851f.up.railway.app'
from subagents.tiktok_publisher import upload_to_tiktok
result = asyncio.run(upload_to_tiktok('/path/to/a/small/vertical/test_clip.mp4', 'test post - ignore'))
print('RESULT:', result)
"
```

Expected: prints a real `https://tiktok.com/@crypto_ai_forex/video/...` URL, not `None`. Note this requires the **web service's `MEDIA_DIRS` change from Step 1 to already be deployed** (the local test still calls the real, deployed `web` over HTTPS) — deploy Step 1+2 together in this task's commit before running this verification, or the `push_media` call will fail with a 404 from `web`.

If it prints `None`, check the log lines this function produces — most likely either the video didn't reach `web` (check `web`'s `MEDIA_DIRS` deployed correctly) or Buffer's `createPost` returned an `InvalidInputError` (its `message` field is logged verbatim — read it, it is normally specific, e.g. the "Video URL is not accessible: HTTP 403..." shape observed during design).

- [ ] **Step 4: Commit and deploy**

```bash
git add subagents/tiktok_publisher.py server.py
git commit -m "feat: add TikTok publishing via Buffer

New subagents/tiktok_publisher.py, not yet wired into the approval
flow (Task 3). Extends server.py's /media endpoint to also serve
videos, since Buffer needs a public URL rather than a file upload.
Verified against the real Buffer API and the real crypto_ai_forex
TikTok channel — see
docs/superpowers/specs/2026-07-10-tiktok-autopublish-design.md."
git push origin main
```

This deploys both `Catapult-Bot` and `web` (the `server.py` change affects `web`). Poll both deployments for `SUCCESS`, then check `exciting-patience` per the Global Constraints note and stop it if it woke up.

---

### Task 3: Wire TikTok into the approval + retry flow

**Files:**
- Modify: `subagents/yt_publisher.py`
- Modify: `parser.py`

**Interfaces:**
- Consumes: `upload_to_tiktok(video_path: str, title: str) -> str | None` from Task 2.
- Produces: `tiktok_retry_pending: dict` (module-level, same shape as the existing `approved_videos` — keyed by `video_id`, values are the same video dict), `save_tiktok_retry_pending()`, `async def retry_tiktok_upload(video_id: str)` — these three are what `parser.py`'s new `/retry_tiktok` command depends on.

- [ ] **Step 1: Add the TikTok import and the retry-pending store to `yt_publisher.py`**

In `subagents/yt_publisher.py`, add the import near the top (after the existing `from telegram.ext import ContextTypes` line):

```python
from subagents.tiktok_publisher import upload_to_tiktok
```

Add the new persistence file constant next to the existing ones (near line 22-25):

```python
TIKTOK_RETRY_FILE = "/data/tiktok_retry_pending.json"
```

Add the new module-level dict next to the existing ones (near line 27-30):

```python
tiktok_retry_pending: dict = {}
```

- [ ] **Step 2: Add `save_tiktok_retry_pending()` and extend `load_pending_videos()`**

Add this function right after the existing `save_approved_videos()` (around line 70):

```python
def save_tiktok_retry_pending():
    try:
        with open(TIKTOK_RETRY_FILE, "w", encoding="utf-8") as f:
            json.dump(tiktok_retry_pending, f, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"Save tiktok retry pending error: {e}")
```

Extend the existing `load_pending_videos()` function (around line 72-87) by adding a third `try` block at the end, mirroring the two already there:

```python
    try:
        if os.path.exists(TIKTOK_RETRY_FILE):
            with open(TIKTOK_RETRY_FILE, "r", encoding="utf-8") as f:
                tiktok_retry_pending.update(json.load(f))
            logger.info(f"Загружено {len(tiktok_retry_pending)} видео на повтор TikTok")
    except Exception as e:
        logger.error(f"Load tiktok retry pending error: {e}")
```

- [ ] **Step 3: Add the shared `_finish_publish` helper**

Add this new function right after `announce_in_telegram` (which currently ends around line 333, right before `async def retry_upload`):

```python
async def _finish_publish(video_id: str, video: dict, youtube_id: str):
    """После успешной загрузки на YouTube: анонсирует в канале, пробует TikTok,
    и шлёт админу сводку по обеим площадкам. Общий код для первого одобрения
    и для /retry_videos."""
    await announce_in_telegram(youtube_id, video["title"], video.get("thumbnail_path"))

    tiktok_url = await upload_to_tiktok(video["video_path"], video["title"])
    status_lines = [f"✅ YouTube: https://youtu.be/{youtube_id}"]
    if tiktok_url:
        status_lines.append(f"✅ TikTok: {tiktok_url}")
        try:
            os.remove(video["video_path"])
        except Exception:
            pass
    else:
        status_lines.append("⚠️ TikTok не удался — /retry_tiktok")
        tiktok_retry_pending[video_id] = video
        save_tiktok_retry_pending()

    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": "<b>Видео опубликовано:</b>\n" + "\n".join(status_lines),
                "parse_mode": "HTML",
            },
        )
```

- [ ] **Step 4: Replace the success path in `handle_video_approval`**

Find this block (currently lines 165-182 — the `if youtube_id:` branch inside `handle_video_approval`):

```python
        youtube_id = await upload_to_youtube(video["video_path"], video["title"], video["description"], video["tags"])
        async with httpx.AsyncClient(timeout=15) as client:
            if youtube_id:
                approved_videos.pop(video_id, None)
                save_approved_videos()
                await announce_in_telegram(youtube_id, video["title"], video.get("thumbnail_path"))
                try:
                    os.remove(video["video_path"])
                except Exception:
                    pass
                await client.post(
                    f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": ADMIN_TG_ID,
                        "text": f"✅ <b>Видео опубликовано!</b>\nhttps://youtu.be/{youtube_id}",
                        "parse_mode": "HTML",
                    },
                )
            else:
                await client.post(
                    f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": ADMIN_TG_ID,
                        "text": "❌ Загрузка на YouTube не удалась. Видео сохранено — попробуй /retry_videos позже.",
                        "parse_mode": "HTML",
                    },
                )
```

Replace it with:

```python
        youtube_id = await upload_to_youtube(video["video_path"], video["title"], video["description"], video["tags"])
        if youtube_id:
            approved_videos.pop(video_id, None)
            save_approved_videos()
            await _finish_publish(video_id, video, youtube_id)
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(
                    f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": ADMIN_TG_ID,
                        "text": "❌ Загрузка на YouTube не удалась. Видео сохранено — попробуй /retry_videos позже.",
                        "parse_mode": "HTML",
                    },
                )
```

- [ ] **Step 5: Simplify `retry_upload` to use the same helper**

Find the existing `retry_upload` function (currently lines 335-357):

```python
async def retry_upload(video_id: str):
    video = approved_videos.get(video_id)
    if not video:
        return
    youtube_id = await upload_to_youtube(video["video_path"], video["title"], video["description"], video["tags"])
    if not youtube_id:
        return
    approved_videos.pop(video_id, None)
    save_approved_videos()
    await announce_in_telegram(youtube_id, video["title"], video.get("thumbnail_path"))
    try:
        os.remove(video["video_path"])
    except Exception:
        pass
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": f"✅ <b>Видео опубликовано (повтор)!</b>\nhttps://youtu.be/{youtube_id}",
                "parse_mode": "HTML",
            },
        )
```

Replace it with:

```python
async def retry_upload(video_id: str):
    video = approved_videos.get(video_id)
    if not video:
        return
    youtube_id = await upload_to_youtube(video["video_path"], video["title"], video["description"], video["tags"])
    if not youtube_id:
        return
    approved_videos.pop(video_id, None)
    save_approved_videos()
    await _finish_publish(video_id, video, youtube_id)
```

- [ ] **Step 6: Add `retry_tiktok_upload`**

Add this new function right after the (now-shortened) `retry_upload`:

```python
async def retry_tiktok_upload(video_id: str):
    video = tiktok_retry_pending.get(video_id)
    if not video:
        return
    tiktok_url = await upload_to_tiktok(video["video_path"], video["title"])
    if not tiktok_url:
        return
    tiktok_retry_pending.pop(video_id, None)
    save_tiktok_retry_pending()
    try:
        os.remove(video["video_path"])
    except Exception:
        pass
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": f"✅ <b>TikTok опубликован (повтор)!</b>\n{tiktok_url}",
                "parse_mode": "HTML",
            },
        )
```

- [ ] **Step 7: Verify `yt_publisher.py` imports cleanly**

```bash
cd "/c/Users/Андрей/catapult-bot-git"
python -c "import subagents.yt_publisher"
```

Expected: no output, no traceback (this repo has no test suite — a clean import is the existing precedent for "does this parse and wire up correctly").

- [ ] **Step 8: Add the `/retry_tiktok` command to `parser.py`**

In `parser.py`, extend the import block (currently lines 36-39):

```python
from subagents.yt_publisher import (
    pending_videos, approved_videos, awaiting_self_record_video,
    save_pending_videos, load_pending_videos, handle_video_approval, handle_video_file,
)
```

Replace with:

```python
from subagents.yt_publisher import (
    pending_videos, approved_videos, awaiting_self_record_video, tiktok_retry_pending,
    save_pending_videos, load_pending_videos, handle_video_approval, handle_video_file,
)
```

Add the new command handler function right after the existing `cmd_retry_videos` (currently lines 116-124):

```python
async def cmd_retry_tiktok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TG_ID:
        return
    if not tiktok_retry_pending:
        await update.message.reply_text("📭 Нет видео для повторной публикации в TikTok.")
        return
    await update.message.reply_text(f"🔄 Повторная публикация {len(tiktok_retry_pending)} видео в TikTok...")
    for video_id in list(tiktok_retry_pending.keys()):
        await yt_publisher.retry_tiktok_upload(video_id)
```

Register the handler next to the existing `retry_videos` registration (currently line 1125):

```python
    parser_app.add_handler(CommandHandler("retry_videos", cmd_retry_videos))
```

Add right after it:

```python
    parser_app.add_handler(CommandHandler("retry_tiktok", cmd_retry_tiktok))
```

- [ ] **Step 9: Verify `parser.py` imports cleanly**

```bash
cd "/c/Users/Андрей/catapult-bot-git"
python -c "import parser"
```

Expected: no traceback. (This will attempt to read env vars / may warn about missing ones locally — that's fine and matches existing behavior for every other module in this repo; a traceback about a missing name like `tiktok_retry_pending` or `cmd_retry_tiktok` is what this step is actually checking for.)

- [ ] **Step 10: Commit and deploy**

```bash
git add subagents/yt_publisher.py parser.py
git commit -m "feat: publish approved Shorts to TikTok, add /retry_tiktok

Wires subagents/tiktok_publisher.py into the existing approval and
YouTube-retry paths via a shared _finish_publish helper. YouTube
success is never rolled back by a TikTok failure — the video stays
in tiktok_retry_pending and /retry_tiktok retries just that half."
git push origin main
```

Then poll the Railway deployment and stop `exciting-patience` if it woke, per the Global Constraints note:

```bash
RAILWAY_CALLER="skill:use-railway@1.3.4" railway deployment list \
  --project 2ffa99f8-afbd-4b52-88cd-23a31a9cb39d \
  --environment 63f574de-9e17-46bd-a5f4-e17f0a576f6f \
  --service 82216bd1-88e4-4581-b1d1-dcc60dc6340d --json
```

Wait for `SUCCESS`, then check + stop `exciting-patience`:

```bash
RAILWAY_CALLER="skill:use-railway@1.3.4" railway deployment list \
  --project 2ffa99f8-afbd-4b52-88cd-23a31a9cb39d \
  --environment 63f574de-9e17-46bd-a5f4-e17f0a576f6f \
  --service e1d6be12-b4b2-45f4-a9cc-f650a8971cc3 --json
# if the latest entry isn't already REMOVED:
RAILWAY_CALLER="skill:use-railway@1.3.4" railway down -y \
  --project 2ffa99f8-afbd-4b52-88cd-23a31a9cb39d \
  --environment 63f574de-9e17-46bd-a5f4-e17f0a576f6f \
  --service e1d6be12-b4b2-45f4-a9cc-f650a8971cc3
```

---

### Task 4: End-to-end manual verification

**Files:** none (verification only)

- [ ] **Step 1: Trigger a real Short and approve it**

In the admin Telegram bot, send `/generate_video`. Wait for the approval message, tap **✅ Одобрить и опубликовать**.

- [ ] **Step 2: Confirm both platforms in the admin chat**

Expect a message of the form:
```
Видео опубликовано:
✅ YouTube: https://youtu.be/...
✅ TikTok: https://tiktok.com/@crypto_ai_forex/video/...
```

Open both links, confirm the video is actually there (and, for TikTok, actually public — not `SELF_ONLY`, since Buffer already carries the audited posting permission).

- [ ] **Step 3: Exercise the TikTok failure + retry path**

Temporarily break TikTok publishing without touching YouTube: on Railway, set `BUFFER_API_KEY` to an obviously invalid value with `--skip-deploys`, then `railway up` to actually apply it without reverting to `main`'s already-current code. Run `/generate_video` again, approve it.

Expect:
```
Видео опубликовано:
✅ YouTube: https://youtu.be/...
⚠️ TikTok не удался — /retry_tiktok
```

Restore the real `BUFFER_API_KEY`, redeploy, then send `/retry_tiktok` in the admin bot. Expect a follow-up `✅ TikTok опубликован (повтор)!` message with a working link.

- [ ] **Step 4: Report results to the user**

Summarize what was verified (with the actual YouTube/TikTok links from Step 2) before considering this plan complete.
