# TikTok Autopublish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After an admin approves a generated Short, publish it to TikTok automatically alongside the existing YouTube upload, per `docs/superpowers/specs/2026-07-10-tiktok-autopublish-design.md`.

**Architecture:** New `subagents/tiktok_publisher.py` module (same shape as `yt_voice.py`/`image_generator.py`: module-level `os.getenv` config, one async function, `None`-on-failure). Wired into the existing approval flow in `subagents/yt_publisher.py` via one shared helper (`_finish_publish`) so both the first-time approval path and the existing YouTube-retry path get TikTok publishing for free. A new `tiktok_retry_pending` dict (JSON-persisted, mirroring the existing `approved_videos` store) tracks videos where YouTube succeeded but TikTok didn't, backing a new `/retry_tiktok` admin command.

**Tech Stack:** Python 3.11, httpx (already a dependency) — no new packages. Upload-Post's REST API (`api.upload-post.com`) via plain HTTP, matching the project's existing style of calling ElevenLabs/JSON2Video without SDKs.

## Global Constraints

- Same Railway service (`Catapult-Bot`), same repo, same admin Telegram bot — no new service.
- TikTok posting must never block or roll back a successful YouTube upload — this was an explicit user decision during brainstorming.
- `upload_to_tiktok` must never raise — it returns `None` on any failure, exactly like `generate_image`, `generate_voiceover`, `render_video`, `upload_to_youtube` in this codebase.
- No automated pytest suite in this repo (confirmed precedent: `docs/superpowers/plans/2026-07-05-youtube-shorts-pipeline.md`, Task 1). Every task below verifies manually — a real API call, an admin Telegram command, or Railway log inspection — consistent with that precedent.
- Every commit in this plan goes straight to `main` and is pushed immediately, matching how every other fix landed tonight (2026-07-09/10) — this repo currently has no long-lived feature branch, and Railway auto-deploys `main`.
- After every push that touches `Catapult-Bot`, check whether the legacy `exciting-patience` Railway service (id `e1d6be12-b4b2-45f4-a9cc-f650a8971cc3`) woke up and is fighting the bot for `getUpdates` — stop it with `railway down -y --service e1d6be12-b4b2-45f4-a9cc-f650a8971cc3` if so (see `project_catapult_bot` memory for the full recurring-incident writeup).

---

### Task 1: Upload-Post account setup + confirm the real API response shape

**Files:** none (external account setup + one throwaway verification script)

**Interfaces:**
- Produces: a confirmed, real JSON response shape from Upload-Post's `/api/upload` endpoint for a TikTok post — Task 2 depends on knowing the exact field path to the resulting post URL, which is not fully documented and must be observed directly rather than guessed.
- Produces: two Railway env vars `UPLOAD_POST_API_KEY`, `UPLOAD_POST_PROFILE` that Task 2 reads via `os.getenv`.

- [ ] **Step 1: Create the Upload-Post account and connect TikTok (manual, in browser)**

1. Go to https://www.upload-post.com/ → sign up (free tier).
2. In the dashboard, find **API Keys** (or Settings → API) and copy the API key.
3. Find **Connect TikTok** (or similar) and go through TikTok's OAuth authorization for the target TikTok account.
4. Note the **profile username** Upload-Post assigns after the TikTok account connects — this is the `user` value the API expects, not your Upload-Post login/email.

- [ ] **Step 2: Probe the API once with a real short video to see the exact response shape**

Use any small local `.mp4` (a few seconds is fine — this post will be visible on the real TikTok account, so either delete it afterward on TikTok or use an obviously-test caption).

```bash
curl -X POST https://api.upload-post.com/api/upload \
  -H "Authorization: Apikey YOUR_API_KEY" \
  -F "user=YOUR_PROFILE_USERNAME" \
  -F "platform[]=tiktok" \
  -F "title=test post - ignore" \
  -F "video=@/path/to/small_test_clip.mp4"
```

Save the full raw JSON response. Identify the exact key path to the TikTok post URL (e.g. `results.tiktok.url`, or `data[0].url`, or similar — the public docs describe the field only as "platform-specific post IDs and URLs upon completion" without giving the literal key names, so this has to come from the real response).

If the response only contains a `request_id`/`job_id` with no URL yet (i.e. the upload is processed asynchronously even without `async_upload: true`), note that too — Task 2's implementation will need to poll a status endpoint instead of trusting the immediate response, and Task 2's steps below have a fallback path for this.

- [ ] **Step 3: Set the two env vars locally and on Railway**

```bash
cd "/c/Users/Андрей/catapult-bot-git"
```

Add to `.env`:
```
UPLOAD_POST_API_KEY=<from Step 1>
UPLOAD_POST_PROFILE=<from Step 1>
```

```bash
RAILWAY_CALLER="skill:use-railway@1.3.4" railway variable set \
  UPLOAD_POST_API_KEY="<value>" \
  UPLOAD_POST_PROFILE="<value>" \
  --project 2ffa99f8-afbd-4b52-88cd-23a31a9cb39d \
  --environment 63f574de-9e17-46bd-a5f4-e17f0a576f6f \
  --service 82216bd1-88e4-4581-b1d1-dcc60dc6340d \
  --skip-deploys --json
```

(`--skip-deploys` because Task 2's commit will trigger the real deploy — no need for two.)

---

### Task 2: `subagents/tiktok_publisher.py`

**Files:**
- Create: `subagents/tiktok_publisher.py`

**Interfaces:**
- Produces: `async def upload_to_tiktok(video_path: str, title: str) -> str | None` — the exact signature Task 3 wires into `yt_publisher.py`. Returns the TikTok post URL on success, `None` on any failure (missing config, missing file, HTTP error, unparseable response) — never raises.

- [ ] **Step 1: Write the module**

Use the exact field path you captured in Task 1, Step 2. The code below assumes `results.tiktok.url` — **replace this path with whatever you actually observed** before running Step 2 below.

```python
"""
Sub-agent: публикация видео в TikTok через Upload-Post (сторонний сервис,
уже прошедший аудит TikTok Content Posting API — без него загрузка через
официальный API TikTok была бы видна только самому аккаунту, не подписчикам).
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

UPLOAD_POST_API_KEY = os.getenv("UPLOAD_POST_API_KEY", "")
UPLOAD_POST_PROFILE = os.getenv("UPLOAD_POST_PROFILE", "")
UPLOAD_POST_URL = "https://api.upload-post.com/api/upload"


async def upload_to_tiktok(video_path: str, title: str) -> str | None:
    """Публикует video_path в TikTok через Upload-Post. Возвращает ссылку на пост или None."""
    if not UPLOAD_POST_API_KEY or not UPLOAD_POST_PROFILE:
        logger.warning("UPLOAD_POST_API_KEY/UPLOAD_POST_PROFILE не заданы — пропускаем публикацию в TikTok")
        return None
    if not os.path.exists(video_path):
        logger.error(f"upload_to_tiktok: файл не найден {video_path}")
        return None
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            with open(video_path, "rb") as video_file:
                resp = await client.post(
                    UPLOAD_POST_URL,
                    headers={"Authorization": f"Apikey {UPLOAD_POST_API_KEY}"},
                    data={"user": UPLOAD_POST_PROFILE, "platform[]": "tiktok", "title": title[:150]},
                    files={"video": (os.path.basename(video_path), video_file, "video/mp4")},
                )
            if resp.status_code >= 400:
                logger.error(f"Upload-Post API error {resp.status_code}: {resp.text[:300]}")
                return None
            data = resp.json()
            url = data.get("results", {}).get("tiktok", {}).get("url")
            if not url:
                logger.error(f"Upload-Post: не удалось найти ссылку на TikTok-пост в ответе: {data}")
                return None
            logger.info(f"✅ Опубликовано в TikTok: {url}")
            return url
    except Exception as e:
        logger.error(f"upload_to_tiktok error: {e}")
        return None
```

- [ ] **Step 2: Verify against the real API (not a mock — this project has no test suite, so this is the test)**

```bash
cd "/c/Users/Андрей/catapult-bot-git"
python -c "
import asyncio, os
os.environ['UPLOAD_POST_API_KEY'] = 'your-key-here'
os.environ['UPLOAD_POST_PROFILE'] = 'your-profile-here'
from subagents.tiktok_publisher import upload_to_tiktok
result = asyncio.run(upload_to_tiktok('/path/to/small_test_clip.mp4', 'test post - ignore'))
print('RESULT:', result)
"
```

Expected: prints a real `https://...` TikTok URL, not `None`. If it prints `None`, check the Railway/local log line it produced (`logger.error` calls above) — most likely the field path guessed in Step 1 doesn't match what you captured in Task 1 Step 2; fix the `.get(...)` chain to match and re-run.

- [ ] **Step 3: Commit**

```bash
git add subagents/tiktok_publisher.py
git commit -m "feat: add TikTok publishing via Upload-Post

New subagents/tiktok_publisher.py, not yet wired into the approval
flow (Task 3). Verified against the real Upload-Post API — see
docs/superpowers/specs/2026-07-10-tiktok-autopublish-design.md."
git push origin main
```

This push won't trigger a meaningful Railway deploy behavior change (the function isn't called from anywhere yet), but it does trigger a redeploy — check `exciting-patience` per the Global Constraints note and stop it if it woke up.

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
✅ TikTok: https://www.tiktok.com/...
```

Open both links, confirm the video is actually there (and, for TikTok, actually public — not `SELF_ONLY`, since Upload-Post already carries the audited posting permission).

- [ ] **Step 3: Exercise the TikTok failure + retry path**

Temporarily break TikTok publishing without touching YouTube: on Railway, set `UPLOAD_POST_API_KEY` to an obviously invalid value (`--skip-deploys`, then `railway up` to actually apply it without reverting to `main`'s already-current code — or simplest, just temporarily rename the var via the Railway dashboard UI). Run `/generate_video` again, approve it.

Expect:
```
Видео опубликовано:
✅ YouTube: https://youtu.be/...
⚠️ TikTok не удался — /retry_tiktok
```

Restore the real `UPLOAD_POST_API_KEY`, redeploy, then send `/retry_tiktok` in the admin bot. Expect a follow-up `✅ TikTok опубликован (повтор)!` message with a working link, and confirm the video is gone from `tiktok_retry_pending` (check via `railway logs` — no `Загружено N видео на повтор TikTok` with N > 0 on the next restart, or just trust the success message since there's no test suite to assert against).

- [ ] **Step 4: Report results to the user**

Summarize what was verified (with the actual YouTube/TikTok links from Step 2) before considering this plan complete.
