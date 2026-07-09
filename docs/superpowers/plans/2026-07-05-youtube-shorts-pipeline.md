# YouTube Shorts Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a YouTube Shorts auto-posting pipeline to the existing `Catapult-Bot` Railway service, per `docs/superpowers/specs/2026-07-05-youtube-shorts-pipeline-design.md`: one auto-generated Short/day (script → ElevenLabs voice → gpt-image-1 stills → JSON2Video render → Telegram admin approval → YouTube upload → Telegram announcement) plus a weekly self-record flow, with zero autonomy beyond what already exists for the TG post queue.

**Architecture:** Five new `subagents/*.py` modules mirroring the existing `tg_publisher.py`/`image_brief.py`/`rewriter.py` shape (pure functions, graceful `None`-on-failure degradation, module-level state + JSON persistence for the approval queue). Two new orchestrator jobs wired into the existing APScheduler instance in `parser.py`. One small, necessary addition not in the original spec: JSON2Video needs public HTTPS URLs for source assets (it does not accept file uploads), so a `/media` endpoint is added to the already-deployed `server.py` FastAPI backend to expose `/data/photos` and `/data/audio` files, gated by a shared-secret token.

**Tech Stack:** Python 3.11, python-telegram-bot 20.7, httpx, apscheduler (all existing). New: `google-api-python-client`, `google-auth` (YouTube Data API v3 — upload + search). ElevenLabs and JSON2Video are called via plain httpx (no SDK), matching the project's existing style.

## Global Constraints

- Same Railway service (`Catapult-Bot`), same repo, same admin Telegram bot (`PARSER_BOT_TOKEN`) — no new service, no new bot token.
- Every video (auto-generated or self-recorded) requires admin approval in Telegram before upload — no autonomy change.
- No automated JSON2Video account rotation/creation — the bot only reads whatever `JSON2VIDEO_API_KEY` is currently set.
- **No background music in this version** (confirmed with user — avoids unverified licensing/Content ID risk on a monetized channel). Only narration + auto-generated burned-in captions. Revisit later if the user supplies a licensed track.
- Media hosting for JSON2Video: via a new `/media` endpoint on `server.py`, not external object storage (confirmed with user).
- **No automated pytest suite** — this repo has none (confirmed in the existing `docs/superpowers/plans/2026-06-30-orchestrator-subagents-refactor.md`, Task 1's Test field: "manual import check (no existing test suite in this repo)"). Every task below uses manual verification (import checks, admin commands, log inspection) instead of `pytest`, consistent with that precedent — this is a deliberate adaptation of the writing-plans default, not a shortcut.
- Every external call (Claude, ElevenLabs, JSON2Video, YouTube) must degrade the same way the existing pipeline does: return `None`/`[]` and skip/log, never raise out of a scheduled job.
- Branch: `feature/youtube-shorts-pipeline`, created off latest `main` (current HEAD: `53664c4`).
- Nothing in this plan pushes to `origin` or deploys to Railway — that is a separate, explicit step the user approves after reviewing the diff.

---

### Task 1: Branch + manual account/credentials setup

**Files:**
- Create: `scripts/get_youtube_refresh_token.py`

**Interfaces:**
- Produces: five new Railway env vars the rest of this plan depends on: `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `JSON2VIDEO_API_KEY`, `MEDIA_SERVE_TOKEN`.

- [ ] **Step 1: Create the branch**

```bash
cd "/c/Users/Андрей/catapult-bot-git"
git checkout main && git pull
git checkout -b feature/youtube-shorts-pipeline
```

- [ ] **Step 2: Create the YouTube channel + Google Cloud project (manual, in browser)**

1. Create (or designate) the Google account for the Crypto/AI/Forex/Catapult brand; create the YouTube channel under it.
2. Go to https://console.cloud.google.com/ → create a new project (e.g. "catapult-youtube-shorts").
3. Enable the **YouTube Data API v3** for that project (APIs & Services → Library).
4. Configure the **OAuth consent screen**: type "External", publishing status "Testing" is fine (only the channel owner will authorize it — no verification needed for personal use).
5. Create an **OAuth Client ID** (APIs & Services → Credentials → Create Credentials → OAuth client ID → Application type: **Desktop app**). Note the client ID and client secret.

- [ ] **Step 3: Write the one-time refresh-token script**

```python
"""
Разовый локальный скрипт: получает YOUTUBE_REFRESH_TOKEN через OAuth-флоу в браузере.
Запускать один раз локально (НЕ в Railway).
Перед запуском: pip install google-auth-oauthlib google-api-python-client
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

CLIENT_ID = input("YOUTUBE_CLIENT_ID: ").strip()
CLIENT_SECRET = input("YOUTUBE_CLIENT_SECRET: ").strip()

flow = InstalledAppFlow.from_client_config(
    {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    },
    scopes=SCOPES,
)
creds = flow.run_local_server(port=0)
print("\nYOUTUBE_REFRESH_TOKEN=" + creds.refresh_token)
```

- [ ] **Step 4: Run it locally and capture the refresh token**

```bash
pip install google-auth-oauthlib google-api-python-client
python scripts/get_youtube_refresh_token.py
```

A browser window opens — log in with the **new channel's** Google account and approve. Copy the printed `YOUTUBE_REFRESH_TOKEN` value somewhere safe (you'll set it on Railway in Task 10).

- [ ] **Step 5: Create the ElevenLabs account**

Sign up at https://elevenlabs.io, pick/clone a voice for narration, note the **Voice ID** (Voice Library → your voice → copy ID) and generate an **API key** (Profile → API Keys). These become `ELEVENLABS_VOICE_ID` and `ELEVENLABS_API_KEY`.

- [ ] **Step 6: Create the JSON2Video account**

Sign up at https://json2video.com on the free 600-credit tier, get the API key from the dashboard. This becomes `JSON2VIDEO_API_KEY`. (Per the spec's non-goal: you manage upgrades/rotation manually — the bot never automates this.)

- [ ] **Step 7: Generate the media-serve token**

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Save the output — this becomes `MEDIA_SERVE_TOKEN` (Task 2 and Task 6 both need it).

- [ ] **Step 8: Commit the script**

```bash
git add scripts/get_youtube_refresh_token.py
git commit -m "chore: add one-time YouTube OAuth refresh-token helper script"
```

---

### Task 2: `/media` endpoint on `server.py`

**Files:**
- Modify: `server.py`
- Test: manual (curl against a running local instance)

**Interfaces:**
- Produces: `GET /media/{kind}/{filename}?token=...` where `kind` is `photos` or `audio`. Returns the file or 403/404.
- Consumes: `MEDIA_SERVE_TOKEN` env var.

- [ ] **Step 1: Add the endpoint**

In `server.py`, after the existing env var block (after the `CATAPULT_API` line, ~line 39), add:

```python
MEDIA_SERVE_TOKEN = os.getenv("MEDIA_SERVE_TOKEN", "")
MEDIA_DIRS = {"photos": "/data/photos", "audio": "/data/audio"}
```

Add the import at the top, next to the other `fastapi` import:

```python
from fastapi.responses import FileResponse
```

Add the route (anywhere among the other `@app.get(...)` routes):

```python
@app.get("/media/{kind}/{filename}")
def get_media(kind: str, filename: str, token: str = ""):
    if not MEDIA_SERVE_TOKEN or token != MEDIA_SERVE_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    directory = MEDIA_DIRS.get(kind)
    if not directory:
        raise HTTPException(status_code=404, detail="Not found")
    safe_name = os.path.basename(filename)
    path = os.path.join(directory, safe_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)
```

`os.path.basename(filename)` strips any `../` — combined with the `MEDIA_DIRS` allowlist, this endpoint can only ever serve files that already exist directly inside `/data/photos` or `/data/audio`.

- [ ] **Step 2: Manual verification**

```bash
mkdir -p /data/photos && echo test > /data/photos/probe.jpg
MEDIA_SERVE_TOKEN=test123 uvicorn server:app --port 8001 &
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8001/media/photos/probe.jpg?token=wrong"   # expect 403
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8001/media/photos/probe.jpg?token=test123"  # expect 200
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8001/media/photos/../../etc/passwd?token=test123"  # expect 404 (basename strips traversal)
kill %1
```

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "feat: add token-gated /media endpoint for serving local assets to JSON2Video"
```

---

### Task 3: Hoist `image_brief.py`'s category style dict to module level

**Files:**
- Modify: `subagents/image_brief.py`

**Interfaces:**
- Produces: `CATEGORY_STYLE` (module-level dict), reused by `yt_script.py` in Task 4 so the video script's image briefs use the same per-category color palette as the TG post images, without duplicating the palette text.
- Consumes: nothing new.

**Why this is in scope:** the spec (Architecture section) says `yt_script.py`'s image-brief generation reuses "`subagents/image_brief.py`'s style-per-category approach" — today that dict is a local variable inside `generate_image_brief()`, so there's nothing to import. This hoists it with no behavior change to `generate_image_brief()` itself.

- [ ] **Step 1: Edit `subagents/image_brief.py`**

Replace:

```python
# ── ТЗ для картинки ───────────────────────────────────────────────────────────
async def generate_image_brief(post_text: str, category: str) -> str:
    category_style = {
        "crypto":   "тёмный фон, неоновые синие и оранжевые цвета, Bitcoin/крипто символика, торговые графики",
        "ai":       "тёмный фон, фиолетовые и голубые цвета, нейронные сети, цифровые паттерны",
        "forex":    "тёмный фон, зелёные и синие цвета, валютные пары, торговые графики",
        "catapult": "тёмный фон, золотые и оранжевые цвета, ракета/запуск, трейдинг платформа",
    }
    style = category_style.get(category, category_style["crypto"])
```

with:

```python
# ── Стиль по категориям (используется и для видео-ТЗ в yt_script.py) ─────────
CATEGORY_STYLE = {
    "crypto":   "тёмный фон, неоновые синие и оранжевые цвета, Bitcoin/крипто символика, торговые графики",
    "ai":       "тёмный фон, фиолетовые и голубые цвета, нейронные сети, цифровые паттерны",
    "forex":    "тёмный фон, зелёные и синие цвета, валютные пары, торговые графики",
    "catapult": "тёмный фон, золотые и оранжевые цвета, ракета/запуск, трейдинг платформа",
}

# ── ТЗ для картинки ───────────────────────────────────────────────────────────
async def generate_image_brief(post_text: str, category: str) -> str:
    style = CATEGORY_STYLE.get(category, CATEGORY_STYLE["crypto"])
```

The rest of `generate_image_brief` is unchanged — only the dict moved and got a name capitalized to signal it's now a public constant.

- [ ] **Step 2: Manual verification**

```bash
python -c "from subagents.image_brief import CATEGORY_STYLE, generate_image_brief; print(CATEGORY_STYLE['crypto'])"
```

Expected: prints the crypto style string, no import errors.

- [ ] **Step 3: Commit**

```bash
git add subagents/image_brief.py
git commit -m "refactor: hoist image_brief category style dict to module level for reuse"
```

---

### Task 4: `subagents/yt_script.py`

**Files:**
- Create: `subagents/yt_script.py`
- Test: manual (direct async call, inspect output)

**Interfaces:**
- Consumes: `CLAUDE_API_KEY`, `CLAUDE_API_URL` from `subagents.rewriter`; `CATEGORY_STYLE` from `subagents.image_brief` (Task 3).
- Produces: `generate_video_script(topic_source: str, category: str) -> dict | None` → `{"narration": str, "image_briefs": list[str]}`. `generate_self_record_script(category: str) -> dict | None` → `{"topic": str, "script": str}`. `generate_video_metadata(topic: str, script_text: str, category: str) -> dict | None` → `{"title": str, "description": str, "tags": list[str]}`.

- [ ] **Step 1: Create `subagents/yt_script.py`**

```python
"""
Sub-agent: сценарий, ТЗ для картинок и метаданные YouTube Shorts через Claude.
"""
import re
import logging

import httpx

from subagents.rewriter import CLAUDE_API_KEY, CLAUDE_API_URL
from subagents.image_brief import CATEGORY_STYLE

logger = logging.getLogger(__name__)

CONTEXT_BY_CATEGORY = {
    "crypto":   "криптовалюты, Bitcoin, блокчейн, DeFi, альткоины",
    "ai":       "искусственный интеллект, нейросети, AI инструменты для заработка",
    "forex":    "Forex, валютные пары, трейдинг, аналитика рынка",
    "catapult": "торговую платформу Catapult Trade",
}

async def _call_claude(prompt: str, max_tokens: int) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            data = resp.json()
            if "content" not in data:
                logger.error(f"Claude error: {data}")
                return None
            return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Claude call error: {e}")
        return None

# ── Сценарий для авто-озвучки ─────────────────────────────────────────────────
async def generate_video_script(topic_source: str, category: str) -> dict | None:
    style = CATEGORY_STYLE.get(category, CATEGORY_STYLE["crypto"])
    context = CONTEXT_BY_CATEGORY.get(category, "финансы")
    prompt = f"""Ты — автор вертикальных YouTube Shorts для канала «Крипта, AI, Forex. Как заработать?» (тот же канал, что и в Telegram @Crypto_AI_Forex).

Сценарий пишется для озвучки диктором (TTS) — только то, что должно прозвучать. Без эмодзи, без HTML-тегов, без ремарок в скобках.
Стиль: живо, по делу, крючок в первые 2 секунды, 90-150 слов (30-60 секунд речи).

Тема: {context}
Стиль картинок: {style}

Исходный материал:
{topic_source[:800]}

Напиши сценарий ролика и 2-4 ТЗ для картинок, которые будут сменять друг друга под озвучку — каждое ТЗ должно соответствовать стилю картинок выше.

Ответь СТРОГО в этом формате, без пояснений:
SCRIPT:
<текст для озвучки>
IMAGE 1: <ТЗ для картинки одним предложением>
IMAGE 2: <ТЗ для картинки одним предложением>
IMAGE 3: <ТЗ для картинки одним предложением>"""

    raw = await _call_claude(prompt, max_tokens=800)
    if not raw:
        return None
    return _parse_script(raw)

def _parse_script(raw: str) -> dict | None:
    script_match = re.search(r"SCRIPT:\s*(.+?)(?=\nIMAGE \d+:|\Z)", raw, re.DOTALL)
    image_matches = re.findall(r"IMAGE \d+:\s*(.+)", raw)
    if not script_match or not image_matches:
        logger.error(f"Не удалось распарсить сценарий: {raw[:300]}")
        return None
    narration = script_match.group(1).strip()
    image_briefs = [m.strip() for m in image_matches]
    if not narration or not image_briefs:
        return None
    return {"narration": narration, "image_briefs": image_briefs}

# ── Сценарий для самозаписи ───────────────────────────────────────────────────
async def generate_self_record_script(category: str) -> dict | None:
    context = CONTEXT_BY_CATEGORY.get(category, "финансы")
    prompt = f"""Ты — автор вертикальных YouTube Shorts для канала «Крипта, AI, Forex. Как заработать?».

Тема: {context}

Придумай тему и напиши сценарий на 30-60 секунд, который автор канала прочитает на камеру сам (живая речь от первого лица, не диктор TTS).
Живо, разговорным языком, крючок в первые 2 секунды.

Ответь СТРОГО в этом формате, без пояснений:
TOPIC: <тема одной строкой>
SCRIPT:
<текст для начитки>"""

    raw = await _call_claude(prompt, max_tokens=500)
    if not raw:
        return None
    topic_match = re.search(r"TOPIC:\s*(.+)", raw)
    script_match = re.search(r"SCRIPT:\s*(.+)", raw, re.DOTALL)
    if not topic_match or not script_match:
        logger.error(f"Не удалось распарсить self-record сценарий: {raw[:300]}")
        return None
    topic = topic_match.group(1).strip()
    script = script_match.group(1).strip()
    if not topic or not script:
        return None
    return {"topic": topic, "script": script}

# ── Метаданные для загрузки на YouTube ────────────────────────────────────────
async def generate_video_metadata(topic: str, script_text: str, category: str) -> dict | None:
    prompt = f"""Ты — автор YouTube Shorts канала «Крипта, AI, Forex. Как заработать?».

Тема ролика: {topic}
Текст ролика: {script_text[:800]}

Напиши для загрузки на YouTube:
1. Название — до 100 символов, цепляющее, без обманного кликбейта
2. Описание — 2-3 предложения + призыв подписаться на Telegram @Crypto_AI_Forex
3. 5-8 тегов через запятую (без #, просто ключевые слова)

Ответь СТРОГО в этом формате:
TITLE: <название>
DESCRIPTION: <описание>
TAGS: <тег1, тег2, тег3>"""

    raw = await _call_claude(prompt, max_tokens=400)
    if not raw:
        return None
    title_match = re.search(r"TITLE:\s*(.+)", raw)
    desc_match = re.search(r"DESCRIPTION:\s*(.+?)(?=\nTAGS:|\Z)", raw, re.DOTALL)
    tags_match = re.search(r"TAGS:\s*(.+)", raw)
    if not title_match or not desc_match:
        logger.error(f"Не удалось распарсить метаданные видео: {raw[:300]}")
        return None
    title = title_match.group(1).strip()[:100]
    description = desc_match.group(1).strip()
    if "#shorts" not in description.lower():
        description += "\n\n#Shorts"
    tags = [t.strip() for t in tags_match.group(1).split(",")] if tags_match else []
    if not title or not description:
        return None
    return {"title": title, "description": description, "tags": tags}
```

- [ ] **Step 2: Manual verification**

Requires `CLAUDE_API_KEY` set in your shell env:

```bash
python -c "
import asyncio
from subagents.yt_script import generate_video_script, generate_self_record_script, generate_video_metadata

async def main():
    s = await generate_video_script('Bitcoin пробил новый максимум на фоне ETF-притоков', 'crypto')
    print('SCRIPT:', s)
    r = await generate_self_record_script('crypto')
    print('SELF-RECORD:', r)
    m = await generate_video_metadata('Bitcoin ETF-приток', s['narration'], 'crypto') if s else None
    print('METADATA:', m)

asyncio.run(main())
"
```

Expected: all three print non-`None` dicts with the documented keys (`narration`/`image_briefs`, `topic`/`script`, `title`/`description`/`tags`). If Claude's output doesn't match the strict format, `None` is logged and returned — re-run once (LLM output is non-deterministic) before treating it as a bug.

- [ ] **Step 3: Commit**

```bash
git add subagents/yt_script.py
git commit -m "feat: add yt_script subagent for Shorts narration, image briefs, and metadata"
```

---

### Task 5: `subagents/yt_voice.py`

**Files:**
- Create: `subagents/yt_voice.py`
- Test: manual (direct async call, inspect output file)

**Interfaces:**
- Consumes: `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` env vars (Task 1).
- Produces: `generate_voiceover(script_text: str, filename: str) -> str | None` — path to a saved mp3, or `None` on failure/missing config.

- [ ] **Step 1: Create `subagents/yt_voice.py`**

```python
"""
Sub-agent: озвучка сценария через ElevenLabs TTS.
"""
import os
import logging

import httpx

logger = logging.getLogger(__name__)

ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
AUDIO_DIR = "/data/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

async def generate_voiceover(script_text: str, filename: str) -> str | None:
    """Генерирует mp3-озвучку через ElevenLabs. Возвращает путь к файлу или None."""
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        logger.warning("ELEVENLABS_API_KEY/ELEVENLABS_VOICE_ID не заданы — пропускаем озвучку")
        return None
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "text": script_text,
                    "model_id": "eleven_multilingual_v2",
                },
            )
            if resp.status_code != 200:
                logger.error(f"ElevenLabs API error {resp.status_code}: {resp.text[:300]}")
                return None

            local_path = f"{AUDIO_DIR}/{filename}.mp3"
            with open(local_path, "wb") as f:
                f.write(resp.content)

            logger.info(f"✅ Озвучка сгенерирована: {local_path}")
            return local_path
    except Exception as e:
        logger.error(f"generate_voiceover error: {e}")
        return None
```

- [ ] **Step 2: Manual verification**

Requires `ELEVENLABS_API_KEY`/`ELEVENLABS_VOICE_ID` set in your shell env:

```bash
python -c "
import asyncio
from subagents.yt_voice import generate_voiceover

async def main():
    path = await generate_voiceover('Привет! Это тестовая озвучка для проверки пайплайна.', 'test_voice')
    print('PATH:', path)

asyncio.run(main())
"
```

Expected: prints a path like `/data/audio/test_voice.mp3`; play the file to confirm it's audible Russian speech. With no env vars set, expect `PATH: None` and a warning log — confirms graceful degradation.

- [ ] **Step 3: Commit**

```bash
git add subagents/yt_voice.py
git commit -m "feat: add yt_voice subagent for ElevenLabs narration"
```

---

### Task 6: `subagents/yt_render.py`

**Files:**
- Create: `subagents/yt_render.py`
- Test: manual (requires Task 2's `/media` endpoint deployed/reachable, and Task 1's JSON2Video key)

**Interfaces:**
- Consumes: `JSON2VIDEO_API_KEY`, `BACKEND_URL`, `MEDIA_SERVE_TOKEN` env vars.
- Produces: `render_video(script_text: str, image_paths: list[str], audio_path: str, filename: str) -> str | None` — path to a saved mp4, or `None` on failure/no-credits/timeout.

- [ ] **Step 1: Create `subagents/yt_render.py`**

```python
"""
Sub-agent: сборка вертикального видео (картинки + озвучка + авто-субтитры) через JSON2Video.
JSON2Video принимает только публичные HTTPS-ссылки на ассеты — локальные файлы
раздаются через /media эндпоинт в server.py (см. Task 2 плана).
"""
import os
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

JSON2VIDEO_API_KEY = os.getenv("JSON2VIDEO_API_KEY", "")
BACKEND_URL         = os.getenv("BACKEND_URL", "https://web-production-9851f.up.railway.app")
MEDIA_SERVE_TOKEN   = os.getenv("MEDIA_SERVE_TOKEN", "")
VIDEOS_DIR = "/data/videos"
os.makedirs(VIDEOS_DIR, exist_ok=True)

WORDS_PER_SECOND = 2.5  # оценка длительности озвучки по числу слов — mp3 не декодируем

def _media_url(kind: str, local_path: str) -> str:
    filename = os.path.basename(local_path)
    return f"{BACKEND_URL}/media/{kind}/{filename}?token={MEDIA_SERVE_TOKEN}"

async def render_video(script_text: str, image_paths: list[str], audio_path: str, filename: str) -> str | None:
    """Рендерит вертикальное видео 1080x1920 через JSON2Video. None при сбое/нехватке кредитов/таймауте."""
    if not JSON2VIDEO_API_KEY:
        logger.warning("JSON2VIDEO_API_KEY не задан — пропускаем рендер видео")
        return None
    if not image_paths or not audio_path:
        logger.warning("render_video: нет картинок или озвучки — пропускаем")
        return None

    total_seconds = max(len(script_text.split()) / WORDS_PER_SECOND, len(image_paths) * 3)
    duration_per_image = total_seconds / len(image_paths)

    movie = {
        "resolution": "custom",
        "width": 1080,
        "height": 1920,
        "scenes": [
            {
                "elements": [{
                    "type": "image",
                    "src": _media_url("photos", path),
                    "duration": duration_per_image,
                    "zoom": 2,
                    "pan": "right" if i % 2 == 0 else "left",
                }]
            }
            for i, path in enumerate(image_paths)
        ],
        "elements": [
            {"type": "audio", "src": _media_url("audio", audio_path), "duration": -1},
            {"type": "subtitles"},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.json2video.com/v2/movies",
                headers={"x-api-key": JSON2VIDEO_API_KEY, "Content-Type": "application/json"},
                json=movie,
            )
            if resp.status_code in (402, 429):
                logger.warning(f"JSON2Video: нет кредитов/лимит ({resp.status_code}) — пропускаем рендер")
                return None
            data = resp.json()
            project_id = data.get("project")
            if not project_id:
                logger.error(f"JSON2Video: не удалось создать проект: {data}")
                return None

            for _ in range(60):  # до ~10 минут ожидания рендера
                await asyncio.sleep(10)
                status_resp = await client.get(
                    "https://api.json2video.com/v2/movies",
                    headers={"x-api-key": JSON2VIDEO_API_KEY},
                    params={"project": project_id},
                )
                movie_status = status_resp.json().get("movie", {})
                status = movie_status.get("status")
                if status == "done":
                    video_resp = await client.get(movie_status["url"])
                    local_path = f"{VIDEOS_DIR}/{filename}.mp4"
                    with open(local_path, "wb") as f:
                        f.write(video_resp.content)
                    logger.info(f"✅ Видео отрендерено: {local_path}")
                    return local_path
                if status in ("error", "timeout"):
                    logger.error(f"JSON2Video render failed: {movie_status.get('message')}")
                    return None

            logger.error("JSON2Video: рендер не завершился за отведённое время")
            return None
    except Exception as e:
        logger.error(f"render_video error: {e}")
        return None
```

- [ ] **Step 2: Manual verification**

Requires `JSON2VIDEO_API_KEY`, `BACKEND_URL` (reachable, running Task 2's endpoint), `MEDIA_SERVE_TOKEN` all set, and at least one real image + the test mp3 from Task 5 already sitting in `/data/photos` / `/data/audio`:

```bash
python -c "
import asyncio
from subagents.yt_render import render_video

async def main():
    path = await render_video(
        'Привет! Это тестовая озвучка для проверки пайплайна.',
        ['/data/photos/probe.jpg'],
        '/data/audio/test_voice.mp3',
        'test_render',
    )
    print('PATH:', path)

asyncio.run(main())
"
```

Expected: after up to a few minutes of polling, prints a path like `/data/videos/test_render.mp4`; open it and confirm it's a vertical video with the image, audible narration, and burned-in captions roughly matching the narration. This is the step most likely to need one real round of debugging against the live JSON2Video API (movie JSON field names were verified against JSON2Video's docs during planning, but haven't been exercised against the live API yet) — expect to iterate here before moving on.

- [ ] **Step 3: Commit**

```bash
git add subagents/yt_render.py
git commit -m "feat: add yt_render subagent for JSON2Video vertical video assembly"
```

---

### Task 7: `subagents/yt_publisher.py`

**Files:**
- Create: `subagents/yt_publisher.py`
- Test: manual (wired into parser.py in Task 10; full flow tested in Task 11)

**Interfaces:**
- Consumes: `get_youtube_service` credentials built from `configure()`'s `youtube_client_id/secret/refresh_token` args; `httpx`; `telegram.Bot`.
- Produces: `pending_videos: dict`, `approved_videos: dict`, `editing_video_title: dict`, `awaiting_self_record_video: dict` (all in-memory; only the first two are persisted — mirrors `tg_publisher.py`'s `pending_posts`/`approved_queue` vs. ephemeral `awaiting_photo`/`editing_post`). `configure(parser_bot_token, admin_tg_id, main_bot_token, channel_id, youtube_client_id, youtube_client_secret, youtube_refresh_token, youtube_category_id, youtube_privacy_status)`. `save_pending_videos()`, `load_pending_videos()`, `save_approved_videos()`. `video_approval_keyboard(video_id) -> dict`. `send_video_for_approval(video_path, title, description, tags, category)`. `handle_video_approval(update, context)` (callback handler for `vapprove_`/`vcancel_`/`vedit_`). `handle_video_title_edit(update, context)` (text handler). `handle_video_file(update, context)` (video-message handler for the self-record path). `get_youtube_service()`. `upload_to_youtube(video_path, title, description, tags) -> str | None`. `announce_in_telegram(youtube_video_id)`. `retry_upload(video_id)`.

- [ ] **Step 1: Create `subagents/yt_publisher.py`**

```python
"""
Sub-agent: approval-цикл для YouTube Shorts + загрузка на YouTube + анонс в TG.
Зеркалит форму tg_publisher.py, но для видео.
"""
import os
import time
import json
import asyncio
import hashlib
import logging

import httpx
from telegram import Bot, InputFile
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

VIDEOS_DIR = "/data/videos"
os.makedirs(VIDEOS_DIR, exist_ok=True)

PENDING_FILE  = "/data/pending_videos.json"
APPROVED_FILE = "/data/approved_videos.json"

pending_videos: dict = {}
approved_videos: dict = {}
editing_video_title: dict = {}
awaiting_self_record_video: dict = {}

PARSER_BOT_TOKEN = None
ADMIN_TG_ID = None
MAIN_BOT_TOKEN = None
CHANNEL_ID = None
YOUTUBE_CLIENT_ID = None
YOUTUBE_CLIENT_SECRET = None
YOUTUBE_REFRESH_TOKEN = None
YOUTUBE_CATEGORY_ID = "22"
YOUTUBE_PRIVACY_STATUS = "public"

def configure(parser_bot_token, admin_tg_id, main_bot_token, channel_id,
              youtube_client_id, youtube_client_secret, youtube_refresh_token,
              youtube_category_id="22", youtube_privacy_status="public"):
    global PARSER_BOT_TOKEN, ADMIN_TG_ID, MAIN_BOT_TOKEN, CHANNEL_ID
    global YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
    global YOUTUBE_CATEGORY_ID, YOUTUBE_PRIVACY_STATUS
    PARSER_BOT_TOKEN = parser_bot_token
    ADMIN_TG_ID = admin_tg_id
    MAIN_BOT_TOKEN = main_bot_token
    CHANNEL_ID = channel_id
    YOUTUBE_CLIENT_ID = youtube_client_id
    YOUTUBE_CLIENT_SECRET = youtube_client_secret
    YOUTUBE_REFRESH_TOKEN = youtube_refresh_token
    YOUTUBE_CATEGORY_ID = youtube_category_id
    YOUTUBE_PRIVACY_STATUS = youtube_privacy_status

def save_pending_videos():
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(pending_videos, f, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"Save pending videos error: {e}")

def save_approved_videos():
    try:
        with open(APPROVED_FILE, "w", encoding="utf-8") as f:
            json.dump(approved_videos, f, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"Save approved videos error: {e}")

def load_pending_videos():
    try:
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                pending_videos.clear()
                pending_videos.update(json.load(f))
            logger.info(f"Загружено {len(pending_videos)} pending видео")
    except Exception as e:
        logger.error(f"Load pending videos error: {e}")
    try:
        if os.path.exists(APPROVED_FILE):
            with open(APPROVED_FILE, "r", encoding="utf-8") as f:
                approved_videos.update(json.load(f))
            logger.info(f"Загружено {len(approved_videos)} approved видео")
    except Exception as e:
        logger.error(f"Load approved videos error: {e}")

# ── Клавиатура одобрения видео ────────────────────────────────────────────────
def video_approval_keyboard(video_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Одобрить и опубликовать", "callback_data": f"vapprove_{video_id}"},
        ], [
            {"text": "✏️ Изменить название", "callback_data": f"vedit_{video_id}"},
            {"text": "❌ Отменить", "callback_data": f"vcancel_{video_id}"},
        ]]
    }

# ── Отправка видео на одобрение ───────────────────────────────────────────────
async def send_video_for_approval(video_path: str, title: str, description: str, tags: list, category: str):
    video_id = f"{category}_{hashlib.md5(title.encode()).hexdigest()[:8]}"
    pending_videos[video_id] = {
        "video_path": video_path,
        "title": title,
        "description": description,
        "tags": tags,
        "category": category,
    }
    save_pending_videos()

    bot = Bot(token=PARSER_BOT_TOKEN)
    with open(video_path, "rb") as video_file:
        await bot.send_video(
            chat_id=ADMIN_TG_ID,
            video=InputFile(video_file),
            caption=(
                f"🎬 <b>Новый Short готов!</b> [{category.upper()}]\n\n"
                f"📌 {title}\n\n{description[:500]}"
            ),
            parse_mode="HTML",
            reply_markup=video_approval_keyboard(video_id),
        )

# ── Обработчик кнопок одобрения видео ─────────────────────────────────────────
async def handle_video_approval(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, video_id = query.data.split("_", 1)
    video = pending_videos.get(video_id)
    if not video:
        await query.edit_message_text("⚠️ Видео не найдено или уже обработано.")
        return

    if action == "vapprove":
        await query.edit_message_text("⏳ Загружаю на YouTube...")
        pending_videos.pop(video_id, None)
        approved_videos[video_id] = video
        save_pending_videos()
        save_approved_videos()

        youtube_id = await upload_to_youtube(video["video_path"], video["title"], video["description"], video["tags"])
        async with httpx.AsyncClient(timeout=15) as client:
            if youtube_id:
                approved_videos.pop(video_id, None)
                save_approved_videos()
                await announce_in_telegram(youtube_id)
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

    elif action == "vedit":
        editing_video_title[ADMIN_TG_ID] = video_id
        await query.edit_message_text("✏️ Пришли новое название видео. Для отмены: /cancel", parse_mode="HTML")

    elif action == "vcancel":
        pending_videos.pop(video_id, None)
        save_pending_videos()
        await query.edit_message_text("❌ Видео отменено.")

# ── Обработчик текста при редактировании названия ────────────────────────────
async def handle_video_title_edit(update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    video_id = editing_video_title.get(admin_id)
    if not video_id:
        return
    video = pending_videos.get(video_id)
    if not video:
        editing_video_title.pop(admin_id, None)
        await update.message.reply_text("⚠️ Видео не найдено.")
        return

    video["title"] = update.message.text[:100]
    editing_video_title.pop(admin_id, None)
    save_pending_videos()
    await update.message.reply_text(
        f"✅ <b>Название обновлено:</b>\n{video['title']}",
        parse_mode="HTML",
        reply_markup=video_approval_keyboard(video_id),
    )

# ── Обработчик видео, присланного админом (самозапись) ───────────────────────
async def handle_video_file(update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    state = awaiting_self_record_video.get(admin_id)
    if not state:
        return

    await update.message.reply_text("⏳ Скачиваю видео на сервер...")
    try:
        video = update.message.video
        file_id = video.file_id
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/getFile",
                params={"file_id": file_id},
            )
            file_path = resp.json()["result"]["file_path"]
            file_resp = await client.get(
                f"https://api.telegram.org/file/bot{PARSER_BOT_TOKEN}/{file_path}"
            )
            local_path = f"{VIDEOS_DIR}/self_{int(time.time())}.mp4"
            with open(local_path, "wb") as f:
                f.write(file_resp.content)

        awaiting_self_record_video.pop(admin_id, None)

        from subagents.yt_script import generate_video_metadata
        metadata = await generate_video_metadata(state["topic"], state["script"], state["category"])
        if not metadata:
            await update.message.reply_text("❌ Не удалось подготовить название/описание. Попробуй прислать видео ещё раз позже.")
            return

        await send_video_for_approval(
            local_path, metadata["title"], metadata["description"], metadata["tags"], state["category"]
        )
    except Exception as e:
        logger.error(f"handle_video_file error: {e}")
        await update.message.reply_text(f"❌ Ошибка при обработке видео: {e}")

# ── YouTube: сервис и загрузка ────────────────────────────────────────────────
def get_youtube_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    import googleapiclient.discovery

    creds = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"],
    )
    creds.refresh(Request())
    return googleapiclient.discovery.build("youtube", "v3", credentials=creds)

def _upload_sync(video_path: str, title: str, description: str, tags: list) -> str:
    from googleapiclient.http import MediaFileUpload

    youtube = get_youtube_service()
    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags,
            "categoryId": YOUTUBE_CATEGORY_ID,
            "defaultLanguage": "ru",
        },
        "status": {
            "privacyStatus": YOUTUBE_PRIVACY_STATUS,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True, chunksize=10 * 1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]

async def upload_to_youtube(video_path: str, title: str, description: str, tags: list) -> str | None:
    if not YOUTUBE_CLIENT_ID or not YOUTUBE_CLIENT_SECRET or not YOUTUBE_REFRESH_TOKEN:
        logger.warning("YouTube OAuth не настроен — пропускаем загрузку")
        return None
    if not os.path.exists(video_path):
        logger.error(f"upload_to_youtube: файл не найден {video_path}")
        return None
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _upload_sync, video_path, title, description, tags)
    except Exception as e:
        logger.error(f"upload_to_youtube error: {e}")
        return None

async def announce_in_telegram(youtube_video_id: str):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": CHANNEL_ID,
                    "text": f"🎬 <b>Новый ролик на YouTube!</b>\n\nhttps://youtu.be/{youtube_video_id}",
                    "parse_mode": "HTML",
                },
            )
    except Exception as e:
        logger.error(f"announce_in_telegram error: {e}")

async def retry_upload(video_id: str):
    video = approved_videos.get(video_id)
    if not video:
        return
    youtube_id = await upload_to_youtube(video["video_path"], video["title"], video["description"], video["tags"])
    if not youtube_id:
        return
    approved_videos.pop(video_id, None)
    save_approved_videos()
    await announce_in_telegram(youtube_id)
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

- [ ] **Step 2: Manual verification (import + queue shape only — full flow is Task 11)**

```bash
python -c "
from subagents.yt_publisher import configure, pending_videos, approved_videos, video_approval_keyboard
configure('x','1','y','@c','cid','csec','rtok')
print(video_approval_keyboard('crypto_abcd1234'))
"
```

Expected: prints the keyboard dict with `vapprove_crypto_abcd1234` / `vedit_crypto_abcd1234` / `vcancel_crypto_abcd1234` callback_data values, no import errors (confirms `google-auth`/`google-api-python-client` imports inside the functions don't break module import — they're deferred to first use, so this works even before Task 10 installs the packages, but Task 10 must install them before `get_youtube_service()` is actually called).

- [ ] **Step 3: Commit**

```bash
git add subagents/yt_publisher.py
git commit -m "feat: add yt_publisher subagent for video approval queue + YouTube upload"
```

---

### Task 8: `subagents/yt_ideas.py`

**Files:**
- Create: `subagents/yt_ideas.py`

**Interfaces:**
- Consumes: `get_youtube_service` from `subagents.yt_publisher` (Task 7).
- Produces: `get_trending_shorts_ideas(category: str) -> list[str]` — never raises, returns `[]` on any failure.

- [ ] **Step 1: Create `subagents/yt_ideas.py`**

```python
"""
Sub-agent: подсказки по темам для YouTube Shorts на основе того, что заходит в нише.
Только чтение — никогда не скачивает и не переиспользует чужие видео/аудио,
только заголовки как вдохновение для темы.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

SEARCH_KEYWORDS = {
    "crypto":   "криптовалюта",
    "ai":       "искусственный интеллект заработок",
    "forex":    "форекс трейдинг",
    "catapult": "crypto трейдинг платформа",
}

def _search_sync(youtube, query: str) -> list:
    response = youtube.search().list(
        part="snippet",
        q=query,
        type="video",
        videoDuration="short",
        order="viewCount",
        maxResults=5,
        relevanceLanguage="ru",
    ).execute()
    return [item["snippet"]["title"] for item in response.get("items", [])]

async def get_trending_shorts_ideas(category: str) -> list:
    from subagents.yt_publisher import get_youtube_service

    query = SEARCH_KEYWORDS.get(category, category)
    try:
        youtube = get_youtube_service()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _search_sync, youtube, query)
    except Exception as e:
        logger.warning(f"get_trending_shorts_ideas error: {e}")
        return []
```

- [ ] **Step 2: Manual verification**

Requires Task 7's `configure()` to have been called with real YouTube credentials (do this after Task 10 wires it into `parser.py`, or call `configure()` manually first):

```bash
python -c "
import asyncio
from subagents.yt_publisher import configure
from subagents.yt_ideas import get_trending_shorts_ideas
import os

configure('x', '1', 'y', '@c', os.environ['YOUTUBE_CLIENT_ID'], os.environ['YOUTUBE_CLIENT_SECRET'], os.environ['YOUTUBE_REFRESH_TOKEN'])

async def main():
    print(await get_trending_shorts_ideas('crypto'))

asyncio.run(main())
"
```

Expected: a list of up to 5 real YouTube Shorts titles about crypto. With bad/missing credentials, expect `[]` and a warning log, not a crash.

- [ ] **Step 3: Commit**

```bash
git add subagents/yt_ideas.py
git commit -m "feat: add yt_ideas subagent for Shorts topic inspiration via YouTube search"
```

---

### Task 9: `orchestrator.py` additions

**Files:**
- Modify: `orchestrator.py`

**Interfaces:**
- Consumes: `collect_top_posts` (existing), `get_trending_shorts_ideas` (Task 8), `generate_video_script`/`generate_self_record_script`/`generate_video_metadata` (Task 4), `generate_voiceover` (Task 5), `generate_image` (existing `image_generator.py`), `render_video` (Task 6), `send_video_for_approval`/`awaiting_self_record_video`/`save_pending_videos` (Task 7).
- Produces: `generate_daily_short()`, `propose_self_record_script()` — both scheduled in Task 10.

- [ ] **Step 1: Add imports**

In `orchestrator.py`, after the existing imports (after the `from subagents.image_generator import generate_image` line):

```python
from subagents.yt_ideas import get_trending_shorts_ideas
from subagents.yt_script import generate_video_script, generate_self_record_script, generate_video_metadata
from subagents.yt_voice import generate_voiceover
from subagents.yt_render import render_video
from subagents.yt_publisher import send_video_for_approval, awaiting_self_record_video, save_pending_videos
```

- [ ] **Step 2: Add rotation state and the two functions**

Right after the existing `# ── Состояние ──` block (after `last_poll_date: str = ""`), add:

```python
short_category_idx: int = 0        # текущая категория для авто-Short
self_record_category_idx: int = 0  # текущая категория для предложения самозаписи
```

At the end of the file, add:

```python
# ── Авто-генерация ежедневного YouTube Short (21:00) ──────────────────────────
async def generate_daily_short():
    global short_category_idx
    logger.info("=== Генерация YouTube Short ===")

    categories = ["crypto", "ai", "forex", "catapult"]
    category = categories[short_category_idx % len(categories)]
    short_category_idx += 1

    posts = await collect_top_posts(category)
    topic_source = posts[0]["text"] if posts else category

    ideas = await get_trending_shorts_ideas(category)
    if ideas:
        topic_source += "\n\nАктуальные форматы в нише сейчас: " + "; ".join(ideas[:3])

    script_data = await generate_video_script(topic_source, category)
    if not script_data:
        logger.warning("generate_daily_short: сбой генерации сценария — пропускаем")
        return

    timestamp = int(datetime.utcnow().timestamp())
    audio_path = await generate_voiceover(script_data["narration"], f"short_{timestamp}")
    if not audio_path:
        logger.warning("generate_daily_short: сбой озвучки — пропускаем")
        return

    image_paths = []
    for i, brief in enumerate(script_data["image_briefs"]):
        path = await generate_image(brief, f"short_{timestamp}_{i}")
        if path:
            image_paths.append(path)
    if not image_paths:
        logger.warning("generate_daily_short: не удалось сгенерировать картинки — пропускаем")
        return

    video_path = await render_video(script_data["narration"], image_paths, audio_path, f"short_{timestamp}")
    if not video_path:
        logger.warning("generate_daily_short: сбой рендера видео — пропускаем")
        return

    metadata = await generate_video_metadata(category, script_data["narration"], category)
    if not metadata:
        logger.warning("generate_daily_short: сбой генерации метаданных — пропускаем")
        return

    await send_video_for_approval(video_path, metadata["title"], metadata["description"], metadata["tags"], category)
    logger.info(f"✅ Short готов и отправлен на одобрение: {category}")

# ── Еженедельное предложение темы для самозаписи (вс, 19:05) ─────────────────
async def propose_self_record_script():
    global self_record_category_idx
    logger.info("=== Предложение темы для самозаписи ===")

    categories = ["crypto", "ai", "forex", "catapult"]
    category = categories[self_record_category_idx % len(categories)]
    self_record_category_idx += 1

    script_data = await generate_self_record_script(category)
    if not script_data:
        logger.warning("propose_self_record_script: сбой генерации — пропускаем")
        return

    awaiting_self_record_video[ADMIN_TG_ID] = {
        "topic": script_data["topic"],
        "script": script_data["script"],
        "category": category,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": (
                    f"🎬 <b>Тема для самозаписи ролика:</b>\n\n"
                    f"📌 {script_data['topic']}\n\n"
                    f"{'─' * 28}\n"
                    f"{script_data['script']}\n"
                    f"{'─' * 28}\n\n"
                    f"Запиши видео на эту тему и пришли файл сюда — я подготовлю название, описание и отправлю на одобрение."
                ),
                "parse_mode": "HTML",
            },
        )
```

- [ ] **Step 3: Manual verification**

```bash
python -c "import orchestrator; print(orchestrator.generate_daily_short, orchestrator.propose_self_record_script)"
```

Expected: no import errors, both print as function objects. Full behavioral test is Task 11 (needs the admin command wired in Task 10).

- [ ] **Step 4: Commit**

```bash
git add orchestrator.py
git commit -m "feat: add generate_daily_short and propose_self_record_script orchestrator jobs"
```

---

### Task 10: `parser.py` wiring + `requirements.txt`

**Files:**
- Modify: `parser.py`, `requirements.txt`

**Interfaces:**
- Consumes: everything from Tasks 4–9.
- Produces: `/generate_video` and `/retry_videos` admin commands; video approval + self-record handlers registered on `parser_app`; scheduled jobs for `generate_daily_short` (daily 21:00) and `propose_self_record_script` (weekly Sun 19:05).

- [ ] **Step 1: Add `google-api-python-client` and `google-auth` to `requirements.txt`**

Append to `requirements.txt`:

```
google-api-python-client==2.149.0
google-auth==2.35.0
```

- [ ] **Step 2: Install locally and confirm no conflicts**

```bash
pip install -r requirements.txt
```

Expected: installs cleanly (bump the two pinned versions if pip reports a resolution conflict with `python-telegram-bot==20.7` or `httpx==0.25.2` — none is expected since these packages don't share transitive deps with the existing stack).

- [ ] **Step 3: Update imports in `parser.py`**

Replace:

```python
from orchestrator import evening_generation, check_breaking_news, PUBLISH_SCHEDULE
```

with:

```python
from orchestrator import evening_generation, check_breaking_news, PUBLISH_SCHEDULE, generate_daily_short, propose_self_record_script
import subagents.yt_publisher as yt_publisher
from subagents.yt_publisher import (
    pending_videos, approved_videos, awaiting_self_record_video,
    save_pending_videos, load_pending_videos, handle_video_approval, handle_video_file,
)
```

- [ ] **Step 4: Route video-title edits inside `handle_edit_message`**

Replace:

```python
    await tg_publisher.handle_admin_edit(update, context)
```

with:

```python
    if user_id in yt_publisher.editing_video_title:
        await yt_publisher.handle_video_title_edit(update, context)
        return

    await tg_publisher.handle_admin_edit(update, context)
```

- [ ] **Step 5: Add the two new admin commands**

Right after `cmd_test_generate` in `parser.py`:

```python
async def cmd_generate_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TG_ID:
        return
    await update.message.reply_text("🎬 Генерирую YouTube Short...")
    await generate_daily_short()

async def cmd_retry_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TG_ID:
        return
    if not approved_videos:
        await update.message.reply_text("📭 Нет видео для повторной загрузки.")
        return
    await update.message.reply_text(f"🔄 Повторная загрузка {len(approved_videos)} видео...")
    for video_id in list(approved_videos.keys()):
        await yt_publisher.retry_upload(video_id)
```

- [ ] **Step 6: Register handlers and `configure()` in `main()`**

Right after the existing `tg_publisher.configure(PARSER_BOT_TOKEN, ADMIN_TG_ID, MAIN_BOT_TOKEN, CHANNEL_ID)` line:

```python
    yt_publisher.configure(
        PARSER_BOT_TOKEN, ADMIN_TG_ID, MAIN_BOT_TOKEN, CHANNEL_ID,
        os.getenv("YOUTUBE_CLIENT_ID", ""), os.getenv("YOUTUBE_CLIENT_SECRET", ""), os.getenv("YOUTUBE_REFRESH_TOKEN", ""),
        os.getenv("YOUTUBE_CATEGORY_ID", "22"), os.getenv("YOUTUBE_PRIVACY_STATUS", "public"),
    )
```

Right after `parser_app.add_handler(CommandHandler("test_generate", cmd_test_generate))`:

```python
    parser_app.add_handler(CommandHandler("generate_video", cmd_generate_video))
    parser_app.add_handler(CommandHandler("retry_videos", cmd_retry_videos))
    parser_app.add_handler(CallbackQueryHandler(handle_video_approval, pattern="^(vapprove|vcancel|vedit)_"))
    parser_app.add_handler(MessageHandler(filters.VIDEO & filters.User(ADMIN_TG_ID), handle_video_file))
```

Right after `scheduler.add_job(check_breaking_news, "interval", hours=1)`:

```python
    # Ежедневная генерация YouTube Short в 21:00 (после вечерней генерации TG-постов в 20:00)
    scheduler.add_job(generate_daily_short, "cron", hour=21, minute=0)

    # Еженедельное предложение темы для самозаписи (вс, 19:05 — сразу после контент-плана в 19:00)
    scheduler.add_job(propose_self_record_script, "cron", day_of_week="sun", hour=19, minute=5)
```

Right after `load_pending()`:

```python
    load_pending_videos()
```

- [ ] **Step 7: Manual verification**

```bash
python -c "import parser" 2>&1 | tail -30
```

Expected: no `ImportError`/`SyntaxError`/`NameError` (parser.py imports orchestrator, which imports all five new subagent modules — this single import exercises the whole chain). Fix any typo/import-order issue before proceeding.

- [ ] **Step 8: Commit**

```bash
git add parser.py requirements.txt
git commit -m "feat: wire YouTube Shorts pipeline into parser.py (commands, handlers, scheduler)"
```

---

## Addendum (post-merge-review): direct upload path for self-recorded videos

**Why:** the self-record path (Task 7/9's `handle_video_file`) receives the video via a Telegram video *message*, which requires the bot to call Telegram's `getFile` to download it — and `getFile` refuses files over **20MB**, a hard limit of the cloud Bot API unrelated to how large a file Telegram will let a user *send*. A typical 30-60s 1080p phone clip is usually under 20MB, but not always (4K, high bitrate, longer clips). Rather than standing up a full self-hosted Local Bot API server (real infra, out of scope), the user chose to add a second, parallel path: a simple authenticated upload page on the already-public `server.py`, so a large clip can be uploaded via ordinary HTTP instead of through Telegram's file-download ceiling. **The existing Telegram-video-message path is kept as-is** (still fine for small clips) — this only adds an alternative for large ones.

**Design:** `propose_self_record_script()` now also mints a single-use, high-entropy upload token (`secrets.token_urlsafe(16)`) and includes an upload link in its Telegram message. `server.py` gets `GET/POST /upload/{token}` — a plain HTML form, no JS — that saves the uploaded bytes to `/data/videos/` and appends a small record to a queue file. Critically, **`server.py` (the `web` process) never touches `pending_videos`/`approved_videos` directly** — those stay single-writer, owned only by the `parser` process, to avoid a cross-process in-memory/disk desync (the `parser` process's `pending_videos` dict is loaded once at startup and only ever mutated from within that same process; if `server.py` wrote to it directly, `parser`'s in-memory copy would silently miss the new entry until a restart). Instead, `server.py` only reads/writes two new disk-file "mailboxes" (`upload_tokens.json`, `pending_uploads.json`) that have no persistent in-memory cache — every access re-reads/rewrites the file directly — and a new `parser`-side polling job (`process_self_record_uploads`, every 1 minute) drains `pending_uploads.json` and does the actual `generate_video_metadata` → `send_video_for_approval` work, entirely inside the `parser` process. This mirrors the existing project pattern of plain-JSON-file state with no locking (acceptable here: one admin, uploads are rare, the collision window between a poll and a concurrent upload is a few milliseconds at most).

**New dependency:** `python-multipart` (required by FastAPI/Starlette to parse `UploadFile` form uploads) — not previously in `requirements.txt`.

---

### Task 11: Upload-token queue in `subagents/yt_publisher.py`

**Files:**
- Modify: `subagents/yt_publisher.py`

**Interfaces:**
- Produces: `create_upload_token(topic: str, script: str, category: str) -> str`, `get_upload_token_info(token: str) -> dict | None`, `consume_upload_token(token: str, video_path: str) -> bool`, `pop_pending_uploads() -> list`.
- Consumes: nothing new — pure file-based, no `configure()` dependency (so `server.py`, a separate process that never calls `yt_publisher.configure()`, can safely call these four functions).

- [ ] **Step 1: Add to `subagents/yt_publisher.py`**

Add `import secrets` alongside the existing `import time` at the top. Add near the other `*_FILE` constants (after `APPROVED_FILE`):

```python
UPLOAD_TOKENS_FILE = "/data/upload_tokens.json"
PENDING_UPLOADS_FILE = "/data/pending_uploads.json"
```

Add at the end of the file:

```python
# ── Токены загрузки для самозаписи (обходим лимит getFile в 20МБ) ───────────
def _read_json_file(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Read {path} error: {e}")
    return default

def _write_json_file(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Write {path} error: {e}")

def create_upload_token(topic: str, script: str, category: str) -> str:
    token = secrets.token_urlsafe(16)
    tokens = _read_json_file(UPLOAD_TOKENS_FILE, {})
    tokens[token] = {"topic": topic, "script": script, "category": category}
    _write_json_file(UPLOAD_TOKENS_FILE, tokens)
    return token

def get_upload_token_info(token: str) -> dict | None:
    tokens = _read_json_file(UPLOAD_TOKENS_FILE, {})
    return tokens.get(token)

def consume_upload_token(token: str, video_path: str) -> bool:
    tokens = _read_json_file(UPLOAD_TOKENS_FILE, {})
    info = tokens.pop(token, None)
    if not info:
        return False
    _write_json_file(UPLOAD_TOKENS_FILE, tokens)

    uploads = _read_json_file(PENDING_UPLOADS_FILE, [])
    uploads.append({
        "video_path": video_path,
        "topic": info["topic"],
        "script": info["script"],
        "category": info["category"],
    })
    _write_json_file(PENDING_UPLOADS_FILE, uploads)
    return True

def pop_pending_uploads() -> list:
    uploads = _read_json_file(PENDING_UPLOADS_FILE, [])
    _write_json_file(PENDING_UPLOADS_FILE, [])
    return uploads
```

- [ ] **Step 2: Manual verification**

```bash
.venv/Scripts/python.exe -c "
from subagents.yt_publisher import create_upload_token, get_upload_token_info, consume_upload_token, pop_pending_uploads
t = create_upload_token('topic', 'script', 'crypto')
assert get_upload_token_info(t) == {'topic': 'topic', 'script': 'script', 'category': 'crypto'}
assert consume_upload_token(t, '/data/videos/x.mp4') is True
assert get_upload_token_info(t) is None, 'token must be single-use'
assert consume_upload_token(t, '/data/videos/x.mp4') is False, 'second consume must fail'
uploads = pop_pending_uploads()
assert uploads == [{'video_path': '/data/videos/x.mp4', 'topic': 'topic', 'script': 'script', 'category': 'crypto'}], uploads
assert pop_pending_uploads() == [], 'must be drained after pop'
print('OK')
"
```

Expected: prints `OK`, no assertion errors.

- [ ] **Step 3: Commit**

```bash
git add subagents/yt_publisher.py
git commit -m "feat: add upload-token queue to yt_publisher for direct video uploads"
```

---

### Task 12: Upload endpoint in `server.py`

**Files:**
- Modify: `server.py`
- Modify: `requirements.txt` (add `python-multipart`)

**Interfaces:**
- Produces: `GET /upload/{token}` (HTML form or "invalid link" page), `POST /upload/{token}` (accepts the file, saves it, calls `yt_publisher.consume_upload_token`).
- Consumes: `subagents.yt_publisher.get_upload_token_info`/`consume_upload_token` (Task 11).

- [ ] **Step 1: Add `python-multipart` to `requirements.txt`**

Append: `python-multipart==0.0.9`

- [ ] **Step 2: Add to `server.py`**

Add near the top, alongside the other imports:

```python
import time
from fastapi import UploadFile, File
from fastapi.responses import HTMLResponse
import subagents.yt_publisher as yt_publisher
```

Add near the other module-level constants:

```python
VIDEOS_DIR = "/data/videos"
os.makedirs(VIDEOS_DIR, exist_ok=True)

UPLOAD_FORM_HTML = """<!doctype html>
<html><body>
<h3>Загрузка ролика</h3>
<form action="" method="post" enctype="multipart/form-data">
<input type="file" name="video" accept="video/*" required>
<button type="submit">Загрузить</button>
</form>
</body></html>"""
```

Add the two routes (anywhere among the other `@app.get`/`@app.post` routes):

```python
@app.get("/upload/{token}", response_class=HTMLResponse)
def upload_form(token: str):
    info = yt_publisher.get_upload_token_info(token)
    if not info:
        return HTMLResponse("<h3>Ссылка недействительна или уже использована.</h3>", status_code=404)
    return HTMLResponse(UPLOAD_FORM_HTML)

@app.post("/upload/{token}", response_class=HTMLResponse)
async def upload_submit(token: str, video: UploadFile = File(...)):
    info = yt_publisher.get_upload_token_info(token)
    if not info:
        return HTMLResponse("<h3>Ссылка недействительна или уже использована.</h3>", status_code=404)

    local_path = f"{VIDEOS_DIR}/self_{int(time.time())}.mp4"
    with open(local_path, "wb") as f:
        f.write(await video.read())

    if not yt_publisher.consume_upload_token(token, local_path):
        return HTMLResponse("<h3>Ссылка уже использована.</h3>", status_code=409)

    return HTMLResponse("<h3>Готово! Видео загружено и обрабатывается.</h3>")
```

- [ ] **Step 3: Install and verify locally**

```bash
.venv/Scripts/pip.exe install python-multipart
.venv/Scripts/python.exe -c "
from subagents.yt_publisher import create_upload_token
print(create_upload_token('t', 's', 'crypto'))
"
```

Take the printed token, then:

```bash
MEDIA_SERVE_TOKEN=test123 .venv/Scripts/python.exe -m uvicorn server:app --port 8001 &
curl -s "http://localhost:8001/upload/bogus-token"                 # expect 404 body "Ссылка недействительна..."
curl -s "http://localhost:8001/upload/<the real token from above>" # expect the HTML upload form
echo "fake video bytes" > /tmp/probe.mp4
curl -s -F "video=@/tmp/probe.mp4" "http://localhost:8001/upload/<the real token>"   # expect success HTML
curl -s -F "video=@/tmp/probe.mp4" "http://localhost:8001/upload/<the real token>"   # expect 409, token already used
kill %1
ls /data/videos/   # confirm a new self_<timestamp>.mp4 exists
```

- [ ] **Step 4: Commit**

```bash
git add server.py requirements.txt
git commit -m "feat: add token-gated /upload endpoint for direct self-record video uploads"
```

---

### Task 13: Wire the upload flow into `orchestrator.py` and `parser.py`

**Files:**
- Modify: `orchestrator.py`
- Modify: `parser.py`

**Interfaces:**
- Consumes: `create_upload_token`, `pop_pending_uploads` (Task 11); `generate_video_metadata` (existing, Task 4); `send_video_for_approval` (existing, Task 7).
- Produces: `process_self_record_uploads()` — scheduled every minute in `parser.py`.

- [ ] **Step 1: `orchestrator.py` — add `BACKEND_URL` and extend the import line**

Near the top, alongside `PARSER_BOT_TOKEN`/`ADMIN_TG_ID`:

```python
BACKEND_URL = os.getenv("BACKEND_URL", "https://web-production-9851f.up.railway.app")
```

Change the existing:
```python
from subagents.yt_publisher import send_video_for_approval, awaiting_self_record_video
```
to:
```python
from subagents.yt_publisher import send_video_for_approval, awaiting_self_record_video, create_upload_token, pop_pending_uploads
```

- [ ] **Step 2: `orchestrator.py` — extend `propose_self_record_script()` and add `process_self_record_uploads()`**

Inside `propose_self_record_script()`, right after the existing `awaiting_self_record_video[ADMIN_TG_ID] = {...}` block and before the `async with httpx.AsyncClient(...)` send, add:

```python
    upload_token = create_upload_token(script_data["topic"], script_data["script"], category)
    upload_url = f"{BACKEND_URL}/upload/{upload_token}"
```

Then change the message `text` to add one more line at the end (right before the closing `),` of the `sendMessage` json), so the full text becomes:

```python
                "text": (
                    f"🎬 <b>Тема для самозаписи ролика:</b>\n\n"
                    f"📌 {script_data['topic']}\n\n"
                    f"{'─' * 28}\n"
                    f"{script_data['script']}\n"
                    f"{'─' * 28}\n\n"
                    f"Запиши видео на эту тему и пришли файл сюда — я подготовлю название, описание и отправлю на одобрение.\n\n"
                    f"📎 Если ролик больше ~15 МБ, Telegram не даст мне его скачать напрямую — вместо этого загрузи его тут: {upload_url}"
                ),
```

At the end of the file, add:

```python
# ── Обработка видео, загруженных через /upload (для роликов больше лимита Telegram) ──
async def process_self_record_uploads():
    uploads = pop_pending_uploads()
    for item in uploads:
        try:
            metadata = await generate_video_metadata(item["topic"], item["script"], item["category"])
            if not metadata:
                logger.warning("process_self_record_uploads: сбой генерации метаданных — пропускаем")
                continue
            await send_video_for_approval(
                item["video_path"], metadata["title"], metadata["description"], metadata["tags"], item["category"]
            )
        except Exception as e:
            logger.error(f"process_self_record_uploads error: {e}")
```

- [ ] **Step 3: `parser.py` — import and schedule**

Change:
```python
from orchestrator import evening_generation, check_breaking_news, PUBLISH_SCHEDULE, load_poll_state, generate_daily_short, propose_self_record_script
```
to:
```python
from orchestrator import evening_generation, check_breaking_news, PUBLISH_SCHEDULE, load_poll_state, generate_daily_short, propose_self_record_script, process_self_record_uploads
```

Right after the existing `scheduler.add_job(propose_self_record_script, "cron", day_of_week="sun", hour=19, minute=5)`:

```python
    # Обработка видео, загруженных через /upload (для самозаписи, раз в минуту)
    scheduler.add_job(process_self_record_uploads, "interval", minutes=1)
```

- [ ] **Step 4: Manual verification**

```bash
.venv/Scripts/python.exe -c "import parser; import orchestrator; print(orchestrator.process_self_record_uploads)"
```

Expected: no import errors, prints the function object.

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py parser.py
git commit -m "feat: wire direct-upload flow for self-recorded videos into scheduler"
```

---

### Task 14: End-to-end manual verification

**Files:** none (verification only)

- [x] **Step 1: Set all new env vars**

Locally (`.env` or exported) and on Railway (`Catapult-Bot` service → Variables): `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `JSON2VIDEO_API_KEY`, `MEDIA_SERVE_TOKEN` (same value on both `web` and `parser` processes — they share `/data` but need the matching token to talk to each other).

- [ ] **Step 2: Deploy to Railway (with user's explicit go-ahead)**

This is the point where the branch actually reaches production — confirm with the user before pushing/merging, per the plan's global constraint that nothing deploys without an explicit approval step.

- [ ] **Step 3: Trigger and approve a real auto-generated Short**

In the admin Telegram bot: send `/generate_video`. Watch the logs (`railway logs` or local run) through script → voice → images → render → approval message. When the video arrives with the approve/edit/cancel keyboard, tap **✅ Одобрить и опубликовать**. Confirm:
- The bot replies with a `youtu.be/...` link.
- The video is visible on the YouTube channel (check `privacyStatus` matches `YOUTUBE_PRIVACY_STATUS`).
- `@Crypto_AI_Forex` received the announcement message with the same link.

- [ ] **Step 4: Exercise the self-record path — both branches**

Manually call `propose_self_record_script()` once (e.g. temporarily via a throwaway admin command, or wait for Sunday 19:05) to receive the topic+script message with the upload link.
- **Small-clip branch:** send a clip well under 20MB directly as a Telegram video message; confirm it goes through metadata generation → approval → (after tapping approve) YouTube upload → announcement.
- **Large-clip branch:** open the `{BACKEND_URL}/upload/{token}` link from the same message on a phone browser, upload a clip (ideally one over 20MB, to actually prove the point), confirm the page shows the success message, then within ~1 minute confirm the bot sends the same approval message as the small-clip case, and that approving it publishes correctly.

- [ ] **Step 5: Exercise the failure paths deliberately**

- Temporarily set `JSON2VIDEO_API_KEY` to an invalid value, run `/generate_video`, confirm the job logs a warning and skips cleanly (no crash, no partial upload) — this is the exact behavior the user chose over automating credit-rotation.
- Temporarily unset `ELEVENLABS_API_KEY`, run `/generate_video`, confirm the same graceful skip at the voice step.
- Restore both real values afterward.

- [ ] **Step 6: Confirm with the user**

Report back what was verified (with actual YouTube/Telegram links) before considering this plan complete.
