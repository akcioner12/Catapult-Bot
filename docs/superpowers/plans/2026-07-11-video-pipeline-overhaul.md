# Video Pipeline Overhaul (Weekly Scheduling + Hot Topics + TikTok Fallback) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Combine three approved specs into one implementation pass: weekly-batch video generation with scheduled auto-publish, broader/hotter topic selection, and a TikTok-safe fallback when the compliance check blocks a video.

**Architecture:** `orchestrator.py` gains a per-slot generation helper (`_generate_and_queue_video`) driven by a new `WEEKLY_SCHEDULE` table (14 entries/week) instead of one video/day; `subagents/yt_publisher.py` gains the schedule constants, a `publish_due_slot` cron target, and a one-shot TikTok fallback path; `subagents/yt_script.py` and `subagents/yt_ideas.py` gain the broader topic-candidate signals and the fallback-script generator. `parser.py`'s scheduler is rewired from 1 daily cron to 1 weekly-batch cron + 8 publish crons.

**Tech Stack:** Python 3.11, `python-telegram-bot`, `apscheduler` (`AsyncIOScheduler`), `httpx`, direct Anthropic Messages API calls (no SDK), JSON-file persistence (no DB).

## Global Constraints

- **No test framework exists in this repo** (no `pytest`, no `tests/` dir, not in `requirements.txt`) — this plan does not introduce one, matching existing project convention. Every task's "test" step is one of: `python -m py_compile <file>` (free, instant syntax check), a direct call to a genuinely free/cheap function (e.g. `get_trending_coins` hits a free public API; a single Claude text call costs a few cents), or a documented manual Telegram/Railway-log check deferred to the final deploy task.
- **Never execute the expensive pipeline steps as a "test."** `generate_voiceover`, `generate_image`, `render_video`, `generate_weekly_batch`, `upload_to_youtube`, `upload_to_tiktok` cost real money (ElevenLabs/Gemini/JSON2Video credits) and/or publish real content. These are only ever triggered deliberately by the operator via the bot's own commands, never as an implementation-verification step.
- Target runtime is Python 3.11.15 (Railway's resolved version) — don't use syntax requiring 3.12+. In particular, an f-string cannot reuse its own outer quote character inside an embedded expression pre-3.12 (e.g. inside `f"""..."""`, use `'single quotes'` for any string literal that appears inside a `{...}` expression).
- All new async functions follow the existing fail-soft convention already used throughout `subagents/`: on error, log via the module's `logger` and return `None`/`[]`, never raise out of the function.
- Timezone is `Europe/Kiev` throughout (matches the existing `AsyncIOScheduler(timezone="Europe/Kiev")` in `parser.py`).
- Because several tasks touch `orchestrator.py` and `parser.py`'s import list together, some intermediate commits between Task 6 and Task 11 will leave `parser.py` unable to `import orchestrator` cleanly (it still references the soon-to-be-renamed `generate_daily_short`). This is expected — nobody runs `parser.py` until the final deploy task (Task 12); each task's own test step verifies only the file(s) it changed via `py_compile`, not a full app boot.
- **Before Task 12 (deploy):** the operator must resolve (approve or cancel, via the live Telegram bot) the 2 pending test videos already sitting in production's `pending_videos.json` from earlier `/generate_video` tests today — they predate the new `planned_day`/`planned_time`/`narration`/`image_paths` fields. The approval-handler code added in Task 8 uses `.get()` with fallbacks so it won't crash on these legacy entries, but resolving them first avoids any confusion about what "✅ В очереди. Выйдет: ?, ?" would mean for a pre-existing video.

---

## File Structure

- Modify `subagents/yt_ideas.py` — add `get_trending_coins()`.
- Modify `subagents/yt_script.py` — rewrite `generate_video_script`'s prompt for the candidate-list framing; add `generate_tiktok_safe_script()`.
- Modify `subagents/yt_publisher.py` — add `WEEKLY_SCHEDULE`, `DAY_NAMES_RU`, `KYIV_TZ`, `lookup_schedule_slot()`; extend `send_video_for_approval()`'s signature; add `notify_admin()` and refactor 2 existing call sites onto it; change `handle_video_approval`'s `vapprove` branch from immediate-publish to queue; add `publish_due_slot()`; add `_attempt_tiktok_fallback()`; modify `_finish_publish()` (drop catapult's hard skip, call the fallback on block); modify `handle_video_file()` (thread `narration` + look up `planned_day`/`planned_time`).
- Modify `orchestrator.py` — replace `generate_daily_short` + `short_category_idx` with `_generate_and_queue_video()` (incorporating the broadened topic-candidate assembly) and `generate_weekly_batch()`; modify `process_self_record_uploads()` (thread `narration` + look up `planned_day`/`planned_time`).
- Modify `parser.py` — swap the `generate_daily_short` import/cron for `generate_weekly_batch`; import and register `publish_due_slot` as 8 new cron jobs.

---

### Task 1: `get_trending_coins()` — CoinGecko trending signal

**Files:**
- Modify: `subagents/yt_ideas.py`

**Interfaces:**
- Produces: `async def get_trending_coins() -> list[str]` — up to 7 strings `"{name} ({symbol})"`, `[]` on any error.

- [ ] **Step 1: Add the function**

Append to `subagents/yt_ideas.py` (after the existing `get_trending_shorts_ideas`, keeping the file's existing `import asyncio` / `import logging` / no-`httpx`-yet-in-this-file situation in mind — this needs `httpx`, so add that import too):

```python
import httpx

COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"

async def get_trending_coins() -> list[str]:
    """Топ монет с резким ростом поискового интереса (CoinGecko /search/trending,
    публичный API, без ключа). [] при любой ошибке/таймауте — не блокирует генерацию."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(COINGECKO_TRENDING_URL)
            data = resp.json()
            coins = data.get("coins", [])[:7]
            return [f'{c["item"]["name"]} ({c["item"]["symbol"].upper()})' for c in coins]
    except Exception as e:
        logger.warning(f"get_trending_coins error: {e}")
        return []
```

Place the `import httpx` line with the other imports at the top of the file (alongside `import asyncio` / `import logging`), not inline.

- [ ] **Step 2: Syntax check**

Run: `python -m py_compile subagents/yt_ideas.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Live check (free API, safe to call directly)**

Run:
```bash
python -c "import asyncio; from subagents.yt_ideas import get_trending_coins; print(asyncio.run(get_trending_coins()))"
```
Expected: a Python list of up to 7 strings like `['Pepe (PEPE)', 'Worldcoin (WLD)', ...]` — real output depends on what's trending right now. An empty list `[]` is also an acceptable pass (means the API call itself didn't crash, worst case CoinGecko rate-limited it) — a Python exception/traceback is the only fail condition.

- [ ] **Step 4: Commit**

```bash
git add subagents/yt_ideas.py
git commit -m "feat: add CoinGecko trending-coins signal for hot topic selection"
```

---

### Task 2: Weekly schedule data model in `yt_publisher.py`

**Files:**
- Modify: `subagents/yt_publisher.py`

**Interfaces:**
- Produces: `WEEKLY_SCHEDULE: list[dict]` (14 entries, keys `day`/`hour`/`minute`/`category`), `DAY_NAMES_RU: dict[str, str]`, `KYIV_TZ` (a `zoneinfo.ZoneInfo`), `lookup_schedule_slot(category: str) -> tuple[str, str]`.
- Modifies: `send_video_for_approval(...)` gains 4 new keyword parameters (`planned_day`, `planned_time`, `narration`, `image_paths`), all stored on the video dict alongside the existing fields.

- [ ] **Step 1: Add the schedule constants**

Add near the top of `subagents/yt_publisher.py`, after the existing module-level constants (`PENDING_FILE`, `APPROVED_FILE`, etc. — around line 29) and before the `pending_videos: dict = {}` state block:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kiev")

WEEKLY_SCHEDULE = [
    {"day": "mon", "hour": 8,  "minute": 30, "category": "forex"},
    {"day": "mon", "hour": 19, "minute": 0,  "category": "crypto"},
    {"day": "tue", "hour": 18, "minute": 30, "category": "ai"},
    {"day": "tue", "hour": 20, "minute": 0,  "category": "catapult"},
    {"day": "wed", "hour": 8,  "minute": 30, "category": "forex"},
    {"day": "wed", "hour": 19, "minute": 0,  "category": "crypto"},
    {"day": "thu", "hour": 18, "minute": 30, "category": "ai"},
    {"day": "thu", "hour": 20, "minute": 0,  "category": "catapult"},
    {"day": "fri", "hour": 8,  "minute": 30, "category": "forex"},
    {"day": "fri", "hour": 19, "minute": 0,  "category": "crypto"},
    {"day": "sat", "hour": 12, "minute": 30, "category": "ai"},
    {"day": "sat", "hour": 14, "minute": 0,  "category": "catapult"},
    {"day": "sun", "hour": 12, "minute": 30, "category": "crypto"},
    {"day": "sun", "hour": 14, "minute": 0,  "category": "ai"},
]

DAY_NAMES_RU = {
    "mon": "понедельник", "tue": "вторник", "wed": "среда", "thu": "четверг",
    "fri": "пятница", "sat": "суббота", "sun": "воскресенье",
}

def lookup_schedule_slot(category: str) -> tuple[str, str]:
    """Возвращает (planned_day, planned_time) первого слота этой категории в
    WEEKLY_SCHEDULE — используется для видео вне обычной генерации (самозапись),
    чисто для отображения админу; не влияет на порядок публикации."""
    entry = next((e for e in WEEKLY_SCHEDULE if e["category"] == category), None)
    if not entry:
        return "", ""
    return entry["day"], f'{entry["hour"]:02d}:{entry["minute"]:02d}'
```

(`datetime` wasn't previously imported in this file — check the existing `import` block at the top and add `from datetime import datetime` there if not already merged into the snippet above; `hashlib`/`secrets`/etc. are already imported.)

- [ ] **Step 2: Extend `send_video_for_approval`'s signature**

Find the current signature (around line 120):
```python
async def send_video_for_approval(video_path: str, title: str, description: str, tags: list, category: str, thumbnail_path: str | None = None):
    video_id = f"{category}_{hashlib.md5(title.encode()).hexdigest()[:8]}"
    pending_videos[video_id] = {
        "video_path": video_path,
        "title": title,
        "description": description,
        "tags": tags,
        "category": category,
        "thumbnail_path": thumbnail_path,
    }
```

Replace with:
```python
async def send_video_for_approval(
    video_path: str, title: str, description: str, tags: list, category: str,
    thumbnail_path: str | None = None,
    planned_day: str = "", planned_time: str = "",
    narration: str = "", image_paths: list[str] | None = None,
):
    video_id = f"{category}_{hashlib.md5(title.encode()).hexdigest()[:8]}"
    pending_videos[video_id] = {
        "video_path": video_path,
        "title": title,
        "description": description,
        "tags": tags,
        "category": category,
        "thumbnail_path": thumbnail_path,
        "planned_day": planned_day,
        "planned_time": planned_time,
        "narration": narration,
        "image_paths": image_paths or [],
    }
```

All 4 new parameters default so every existing call site keeps working unchanged until later tasks update them.

- [ ] **Step 3: Syntax check**

Run: `python -m py_compile subagents/yt_publisher.py`
Expected: no output, exit code 0.

- [ ] **Step 4: Local shape check (no secrets/network needed)**

```bash
python -c "
from subagents.yt_publisher import WEEKLY_SCHEDULE, DAY_NAMES_RU, lookup_schedule_slot
assert len(WEEKLY_SCHEDULE) == 14
from collections import Counter
counts = Counter(e['category'] for e in WEEKLY_SCHEDULE)
assert counts == Counter({'crypto': 4, 'ai': 4, 'forex': 3, 'catapult': 3}), counts
assert set(DAY_NAMES_RU) == {'mon','tue','wed','thu','fri','sat','sun'}
assert lookup_schedule_slot('forex') == ('mon', '08:30')
assert lookup_schedule_slot('nope') == ('', '')
print('OK')
"
```
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add subagents/yt_publisher.py
git commit -m "feat: add WEEKLY_SCHEDULE and extend video dict with planned/narration/image fields"
```

---

### Task 3: `notify_admin()` helper

**Files:**
- Modify: `subagents/yt_publisher.py`

**Interfaces:**
- Produces: `async def notify_admin(text: str) -> None`.

- [ ] **Step 1: Add the helper**

Add near the top of `subagents/yt_publisher.py`, after the `configure()` function (around line 62):

```python
async def notify_admin(text: str):
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_TG_ID, "text": text, "parse_mode": "HTML"},
        )
```

- [ ] **Step 2: Refactor `_finish_publish`'s final summary message onto it**

Find (around line 376):
```python
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
Replace with:
```python
    await notify_admin("<b>Видео опубликовано:</b>\n" + "\n".join(status_lines))
```

- [ ] **Step 3: Refactor `retry_tiktok_upload`'s success message onto it**

Find (around line 410):
```python
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
Replace with:
```python
    await notify_admin(f"✅ <b>TikTok опубликован (повтор)!</b>\n{tiktok_url}")
```

- [ ] **Step 4: Syntax check**

Run: `python -m py_compile subagents/yt_publisher.py`
Expected: no output, exit code 0.

- [ ] **Step 5: Commit**

```bash
git add subagents/yt_publisher.py
git commit -m "refactor: extract notify_admin() helper, dedupe 2 sendMessage call sites"
```

---

### Task 4: Broaden `generate_video_script`'s prompt for hot topics

**Files:**
- Modify: `subagents/yt_script.py`

**Interfaces:**
- `generate_video_script(topic_source: str, category: str) -> dict | None` — signature and return shape (`{"narration": ..., "image_briefs": ...}`) unchanged; only the prompt text and truncation length change. Callers will start passing a multi-candidate block instead of a single post's text starting in Task 6.

- [ ] **Step 1: Rewrite the prompt**

In `subagents/yt_script.py`, find `generate_video_script` (lines 55-76):
```python
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
```

Replace with:
```python
async def generate_video_script(topic_source: str, category: str) -> dict | None:
    style = CATEGORY_STYLE.get(category, CATEGORY_STYLE["crypto"])
    context = CONTEXT_BY_CATEGORY.get(category, "финансы")
    prompt = f"""Ты — автор вертикальных YouTube Shorts для канала «Крипта, AI, Forex. Как заработать?» (тот же канал, что и в Telegram @Crypto_AI_Forex).

Сценарий пишется для озвучки диктором (TTS) — только то, что должно прозвучать. Без эмодзи, без HTML-тегов, без ремарок в скобках.
Стиль: живо, по делу, крючок в первые 2 секунды, 90-150 слов (30-60 секунд речи).

Тема: {context}
Стиль картинок: {style}

Кандидаты на тему за последние часы (посты из каналов, тренды YouTube Shorts{', резко растущие монеты' if category == 'crypto' else ''}):
{topic_source[:1500]}

Выбери ОДНУ самую резонансную, горячую историю из кандидатов выше и напиши сценарий именно про неё — не пытайся смешать несколько тем в одну. Если ни один кандидат не выглядит по-настоящему интересным, возьми {context} как тему в целом.

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
```

(Only the body of the f-string and the `[:800]` → `[:1500]` truncation changed; `_parse_script` and everything else in the file is untouched.)

- [ ] **Step 2: Syntax check**

Run: `python -m py_compile subagents/yt_script.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Live check (small, cheap Claude call — a few cents)**

```bash
python -c "
import asyncio
from dotenv import load_dotenv; load_dotenv()
from subagents.yt_script import generate_video_script
result = asyncio.run(generate_video_script(
    '[TG] Bitcoin пробил \$70000 на фоне новостей об ETF\n[YouTube] Почему BTC растёт прямо сейчас\n[Trending coin] Bitcoin (BTC)',
    'crypto'
))
print(result)
assert result and result.get('narration') and result.get('image_briefs')
print('OK')
"
```
Expected: prints the parsed `{'narration': ..., 'image_briefs': [...]}` dict, then `OK`. Requires `CLAUDE_API_KEY` in the local `.env` (project already depends on `python-dotenv`; this call loads it explicitly since the app itself doesn't call `load_dotenv()` anywhere — no other file needs changing for this, it's a one-off in the verification command).

- [ ] **Step 4: Commit**

```bash
git add subagents/yt_script.py
git commit -m "feat: rewrite generate_video_script prompt to pick from multiple hot-topic candidates"
```

---

### Task 5: `generate_tiktok_safe_script()` — fallback script generator

**Files:**
- Modify: `subagents/yt_script.py`

**Interfaces:**
- Produces: `async def generate_tiktok_safe_script(original_narration: str, category: str, block_reason: str) -> dict | None` — returns `{"narration": ..., "caption": ...}` or `None`.

- [ ] **Step 1: Add the function**

Append to `subagents/yt_script.py`, after `generate_video_metadata`:

```python
# ── Более лояльная версия сценария под TikTok (fallback после блока) ─────────
async def generate_tiktok_safe_script(original_narration: str, category: str, block_reason: str) -> dict | None:
    context = CONTEXT_BY_CATEGORY.get(category, "финансы")
    prompt = f"""Ты — редактор, адаптирующий сценарий ролика под более строгие правила TikTok о финансовом контенте.

Тема: {context}

Оригинальный текст ролика:
{original_narration[:800]}

Модератор TikTok отклонил этот контент по причине: {block_reason}

Перепиши текст в нейтральную, информационную подачу той же темы — как новость или комментарий рынка, без промо-формулировок, без упоминания конкретных продуктов/платформ, без призывов к действию ("вложи", "заработай", "успей"). Сохрани суть темы и факты, но убери всё, что могло вызвать отклонение.

Также напиши короткий caption для поста в TikTok (1-2 предложения, без хэштегов).

Ответь СТРОГО в этом формате, без пояснений:
NARRATION:
<новый текст для озвучки>
CAPTION:
<текст подписи для TikTok>"""

    raw = await _call_claude(prompt, max_tokens=600)
    if not raw:
        return None
    narration_match = re.search(r"NARRATION:\s*(.+?)(?=\nCAPTION:|\Z)", raw, re.DOTALL)
    caption_match = re.search(r"CAPTION:\s*(.+)", raw, re.DOTALL)
    if not narration_match or not caption_match:
        logger.error(f"Не удалось распарсить TikTok-safe сценарий: {raw[:300]}")
        return None
    narration = narration_match.group(1).strip()
    caption = caption_match.group(1).strip()
    if not narration or not caption:
        return None
    return {"narration": narration, "caption": caption}
```

- [ ] **Step 2: Syntax check**

Run: `python -m py_compile subagents/yt_script.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Live check (small, cheap Claude call)**

```bash
python -c "
import asyncio
from dotenv import load_dotenv; load_dotenv()
from subagents.yt_script import generate_tiktok_safe_script
result = asyncio.run(generate_tiktok_safe_script(
    'Заходи в MRKT прямо сейчас и получай Lucky Cards за стейкинг — успей до конца недели!',
    'crypto',
    'Продвижение конкретного крипто-игрового продукта — это реклама платформы, а не новостной комментарий о рынке.'
))
print(result)
assert result and result.get('narration') and result.get('caption')
print('OK')
"
```
Expected: prints `{'narration': ..., 'caption': ...}` with no promotional language, then `OK`.

- [ ] **Step 4: Commit**

```bash
git add subagents/yt_script.py
git commit -m "feat: add generate_tiktok_safe_script for TikTok compliance fallback"
```

---

### Task 6: `orchestrator.py` — replace `generate_daily_short` with weekly batch generation

**Files:**
- Modify: `orchestrator.py`

**Interfaces:**
- Consumes: `WEEKLY_SCHEDULE` from `subagents.yt_publisher` (Task 2); `get_trending_coins` from `subagents.yt_ideas` (Task 1); `send_video_for_approval(..., planned_day=, planned_time=, narration=, image_paths=)` (Task 2).
- Produces: `async def _generate_and_queue_video(category: str, planned_day: str, planned_time: str) -> None`; `async def generate_weekly_batch() -> None`. Removes: `generate_daily_short`, `short_category_idx`.

- [ ] **Step 1: Update imports**

In `orchestrator.py`, change:
```python
from subagents.yt_ideas import get_trending_shorts_ideas
```
to:
```python
from subagents.yt_ideas import get_trending_shorts_ideas, get_trending_coins
```
and change:
```python
from subagents.yt_publisher import send_video_for_approval, awaiting_self_record_video, create_upload_token, pop_pending_uploads
```
to:
```python
from subagents.yt_publisher import send_video_for_approval, awaiting_self_record_video, create_upload_token, pop_pending_uploads, WEEKLY_SCHEDULE
```

- [ ] **Step 2: Remove `short_category_idx`**

Delete this line from the "Состояние" block (around line 81):
```python
short_category_idx: int = 0        # текущая категория для авто-Short
```

- [ ] **Step 3: Replace `generate_daily_short` with `_generate_and_queue_video` + `generate_weekly_batch`**

Find and delete the entire `generate_daily_short` function (lines 380-427, from the `# ── Авто-генерация...` comment through the final `logger.info` line). Replace it with:

```python
# ── Еженедельная генерация 14 видео (вс, 19:10) ───────────────────────────────
async def _generate_and_queue_video(category: str, planned_day: str, planned_time: str):
    posts = await collect_top_posts(category)

    if category == "catapult":
        topic_source = posts[0]["text"] if posts else category
    else:
        candidates = [f"[TG] {p['text'][:200]}" for p in posts[:5]]
        ideas = await get_trending_shorts_ideas(category)
        candidates += [f"[YouTube] {t}" for t in ideas]
        if category == "crypto":
            coins = await get_trending_coins()
            candidates += [f"[Trending coin] {c}" for c in coins]
        topic_source = "\n".join(candidates) if candidates else category

    script_data = await generate_video_script(topic_source, category)
    if not script_data:
        logger.warning(f"_generate_and_queue_video[{category}]: сбой генерации сценария — пропускаем")
        return

    timestamp = int(datetime.utcnow().timestamp())
    audio_path = await generate_voiceover(script_data["narration"], f"short_{timestamp}")
    if not audio_path:
        logger.warning(f"_generate_and_queue_video[{category}]: сбой озвучки — пропускаем")
        return

    image_paths = []
    for i, brief in enumerate(script_data["image_briefs"]):
        path = await generate_image(brief, f"short_{timestamp}_{i}", aspect_ratio="9:16")
        if path:
            image_paths.append(path)
    if not image_paths:
        logger.warning(f"_generate_and_queue_video[{category}]: не удалось сгенерировать картинки — пропускаем")
        return

    video_path = await render_video(script_data["narration"], image_paths, audio_path, f"short_{timestamp}")
    if not video_path:
        logger.warning(f"_generate_and_queue_video[{category}]: сбой рендера видео — пропускаем")
        return

    metadata = await generate_video_metadata(category, script_data["narration"], category)
    if not metadata:
        logger.warning(f"_generate_and_queue_video[{category}]: сбой генерации метаданных — пропускаем")
        return

    await send_video_for_approval(
        video_path, metadata["title"], metadata["description"], metadata["tags"], category,
        thumbnail_path=image_paths[0],
        planned_day=planned_day, planned_time=planned_time,
        narration=script_data["narration"], image_paths=image_paths,
    )
    logger.info(f"✅ Видео готово и отправлено на одобрение: {category} ({planned_day} {planned_time})")

async def generate_weekly_batch():
    logger.info("=== Еженедельная генерация 14 видео ===")
    for entry in WEEKLY_SCHEDULE:
        planned_time = f'{entry["hour"]:02d}:{entry["minute"]:02d}'
        await _generate_and_queue_video(entry["category"], entry["day"], planned_time)
        await asyncio.sleep(2)
```

- [ ] **Step 4: Syntax check**

Run: `python -m py_compile orchestrator.py`
Expected: no output, exit code 0. (This will pass even though `parser.py` now has a dangling reference to the old `generate_daily_short` name — that's expected per Global Constraints, fixed in Task 11.)

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py
git commit -m "feat: replace generate_daily_short with weekly-batch generation across WEEKLY_SCHEDULE"
```

---

### Task 7: Self-record — thread `narration` and look up planned slot

**Files:**
- Modify: `subagents/yt_publisher.py` (`handle_video_file`)
- Modify: `orchestrator.py` (`process_self_record_uploads`)

**Interfaces:**
- Consumes: `lookup_schedule_slot` (Task 2, same module for `handle_video_file`; imported for `process_self_record_uploads`), `notify_admin` (Task 3).

- [ ] **Step 1: Update `handle_video_file` in `subagents/yt_publisher.py`**

Find (around line 256-264):
```python
        from subagents.yt_script import generate_video_metadata
        metadata = await generate_video_metadata(state["topic"], state["script"], state["category"])
        if not metadata:
            await update.message.reply_text("❌ Не удалось подготовить название/описание. Попробуй прислать видео ещё раз позже.")
            return

        await send_video_for_approval(
            local_path, metadata["title"], metadata["description"], metadata["tags"], state["category"]
        )
```
Replace with:
```python
        from subagents.yt_script import generate_video_metadata
        metadata = await generate_video_metadata(state["topic"], state["script"], state["category"])
        if not metadata:
            await update.message.reply_text("❌ Не удалось подготовить название/описание. Попробуй прислать видео ещё раз позже.")
            return

        planned_day, planned_time = lookup_schedule_slot(state["category"])
        await send_video_for_approval(
            local_path, metadata["title"], metadata["description"], metadata["tags"], state["category"],
            planned_day=planned_day, planned_time=planned_time, narration=state["script"],
        )
```
(The function-local `from subagents.yt_script import generate_video_metadata` import is kept as-is here — Task 10 adds a module-level import of the same name later and removes this now-redundant local one then, so this task stays correct and independently runnable on its own.)

- [ ] **Step 2: Update `process_self_record_uploads` in `orchestrator.py`**

Import `lookup_schedule_slot` — extend the existing yt_publisher import line from Task 6, Step 1, to:
```python
from subagents.yt_publisher import send_video_for_approval, awaiting_self_record_video, create_upload_token, pop_pending_uploads, WEEKLY_SCHEDULE, lookup_schedule_slot, notify_admin
```

Find (around lines 471-490):
```python
async def process_self_record_uploads():
    uploads = pop_pending_uploads()
    for item in uploads:
        try:
            metadata = await generate_video_metadata(item["topic"], item["script"], item["category"])
            if not metadata:
                logger.warning("process_self_record_uploads: сбой генерации метаданных — пропускаем")
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": ADMIN_TG_ID,
                            "text": "⚠️ Не удалось обработать загруженное видео (сбой генерации названия/описания). Файл сохранён на сервере, но не отправлен на одобрение — попробуй загрузить его ещё раз.",
                            "parse_mode": "HTML",
                        },
                    )
                continue
            await send_video_for_approval(
                item["video_path"], metadata["title"], metadata["description"], metadata["tags"], item["category"]
            )
        except Exception as e:
            logger.error(f"process_self_record_uploads error: {e}")
```
Replace with:
```python
async def process_self_record_uploads():
    uploads = pop_pending_uploads()
    for item in uploads:
        try:
            metadata = await generate_video_metadata(item["topic"], item["script"], item["category"])
            if not metadata:
                logger.warning("process_self_record_uploads: сбой генерации метаданных — пропускаем")
                await notify_admin("⚠️ Не удалось обработать загруженное видео (сбой генерации названия/описания). Файл сохранён на сервере, но не отправлен на одобрение — попробуй загрузить его ещё раз.")
                continue
            planned_day, planned_time = lookup_schedule_slot(item["category"])
            await send_video_for_approval(
                item["video_path"], metadata["title"], metadata["description"], metadata["tags"], item["category"],
                planned_day=planned_day, planned_time=planned_time, narration=item["script"],
            )
        except Exception as e:
            logger.error(f"process_self_record_uploads error: {e}")
```

- [ ] **Step 3: Syntax check**

Run: `python -m py_compile subagents/yt_publisher.py orchestrator.py`
Expected: no output, exit code 0.

- [ ] **Step 4: Commit**

```bash
git add subagents/yt_publisher.py orchestrator.py
git commit -m "feat: thread self-record script through as narration, look up planned slot"
```

---

### Task 8: Approval no longer publishes immediately

**Files:**
- Modify: `subagents/yt_publisher.py` (`handle_video_approval`)

- [ ] **Step 1: Replace the `vapprove` branch**

Find (around lines 177-198):
```python
    if action == "vapprove":
        await _edit_status(query, "⏳ Загружаю на YouTube...")
        pending_videos.pop(video_id, None)
        approved_videos[video_id] = video
        save_pending_videos()
        save_approved_videos()

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
Replace with:
```python
    if action == "vapprove":
        pending_videos.pop(video_id, None)
        approved_videos[video_id] = video
        save_pending_videos()
        save_approved_videos()
        day_ru = DAY_NAMES_RU.get(video.get("planned_day", ""), "?")
        time_str = video.get("planned_time") or "?"
        await _edit_status(query, f"✅ В очереди. Выйдет: {day_ru}, {time_str}")
```

(`upload_to_youtube` is still used elsewhere in this file — by `publish_due_slot` in Task 9 and `retry_upload` — so its import stays; only this call site changes.)

- [ ] **Step 2: Syntax check**

Run: `python -m py_compile subagents/yt_publisher.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add subagents/yt_publisher.py
git commit -m "feat: approving a video queues it instead of publishing immediately"
```

---

### Task 9: `publish_due_slot()` — scheduled publish from the queue

**Files:**
- Modify: `subagents/yt_publisher.py`

**Interfaces:**
- Consumes: `notify_admin` (Task 3), `KYIV_TZ` (Task 2), `approved_videos`, `upload_to_youtube`, `_finish_publish` (all pre-existing in this file), `save_approved_videos` (pre-existing).
- Produces: `async def publish_due_slot(category: str) -> None`.

- [ ] **Step 1: Add the function**

Add after `retry_upload` (around line 396):

```python
# ── Публикация по расписанию (крон дёргает раз в слот) ───────────────────────
async def publish_due_slot(category: str):
    """Публикует самое старое одобренное видео этой категории. Самовосстанавливается:
    если к моменту слота ничего не одобрено — пропускаем с уведомлением, следующий
    такой же слот заберёт то, что будет одобрено к тому времени."""
    candidates = [(vid, v) for vid, v in approved_videos.items() if v["category"] == category]
    if not candidates:
        now = datetime.now(KYIV_TZ)
        await notify_admin(f"⚠️ Не было одобренного {category}-видео к {now:%H:%M} — слот пропущен.")
        return

    video_id, video = candidates[0]
    approved_videos.pop(video_id, None)
    save_approved_videos()

    youtube_id = await upload_to_youtube(video["video_path"], video["title"], video["description"], video["tags"])
    if youtube_id:
        await _finish_publish(video_id, video, youtube_id)
    else:
        approved_videos[video_id] = video
        save_approved_videos()
        await notify_admin("❌ Загрузка на YouTube не удалась для запланированного видео. Сохранено — попробуй /retry_videos позже.")
```

- [ ] **Step 2: Syntax check**

Run: `python -m py_compile subagents/yt_publisher.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Local logic check (no network — exercises the queue-picking + empty-queue paths only)**

```bash
python -c "
import asyncio
import subagents.yt_publisher as yp

yp.PARSER_BOT_TOKEN = 'dummy'
yp.ADMIN_TG_ID = 0

sent = []
async def fake_notify(text):
    sent.append(text)
yp.notify_admin = fake_notify

# empty queue -> should notify, not raise
asyncio.run(yp.publish_due_slot('crypto'))
assert len(sent) == 1 and 'пропущен' in sent[0], sent
print('OK empty-queue path')
"
```
Expected: `OK empty-queue path`. (This deliberately doesn't exercise the non-empty branch, since that calls the real `upload_to_youtube` — covered instead by the end-to-end manual check in Task 12.)

- [ ] **Step 4: Commit**

```bash
git add subagents/yt_publisher.py
git commit -m "feat: add publish_due_slot for scheduled queue-draining publish"
```

---

### Task 10: TikTok-safe fallback

**Files:**
- Modify: `subagents/yt_publisher.py`

**Interfaces:**
- Consumes: `generate_tiktok_safe_script` (Task 5), `check_tiktok_compliance` (pre-existing import), `upload_to_tiktok` (pre-existing import).
- Produces: `async def _attempt_tiktok_fallback(video_id: str, video: dict, block_reason: str) -> tuple[str, str] | None`.
- Modifies: `_finish_publish` — drops the catapult hard-skip, calls the fallback when blocked.

- [ ] **Step 1: Add module-level imports**

At the top of `subagents/yt_publisher.py`, alongside the existing:
```python
from subagents.tiktok_publisher import upload_to_tiktok
from subagents.tiktok_moderation import check_tiktok_compliance
```
add:
```python
from subagents.yt_script import generate_video_metadata, generate_tiktok_safe_script
from subagents.yt_voice import generate_voiceover
from subagents.yt_render import render_video
```
`handle_video_file` (touched in Task 7) still has its own function-local `from subagents.yt_script import generate_video_metadata` line right before its `metadata = await generate_video_metadata(...)` call — now that this name is imported at module level, delete that local import line; the call itself is unchanged.

- [ ] **Step 2: Add `_attempt_tiktok_fallback`**

Add just before `_finish_publish` (around line 345):

```python
async def _attempt_tiktok_fallback(video_id: str, video: dict, block_reason: str) -> tuple[str, str] | None:
    """Одна попытка более лояльной версии под TikTok. (tiktok_url, note) при успехе,
    None если не получилось (сбой генерации/рендера, или повторный блок) — без
    повторных попыток."""
    fallback = await generate_tiktok_safe_script(video.get("narration", ""), video["category"], block_reason)
    if not fallback:
        return None

    if video.get("image_paths"):
        audio_path = await generate_voiceover(fallback["narration"], f"{video_id}_tt")
        if not audio_path:
            return None
        video_path = await render_video(fallback["narration"], video["image_paths"], audio_path, f"{video_id}_tt")
        if not video_path:
            return None
    else:
        video_path = video["video_path"]

    recheck = await check_tiktok_compliance(fallback["caption"])
    if recheck:
        return None

    tiktok_url = await upload_to_tiktok(video_path, fallback["caption"])
    if not tiktok_url:
        return None

    if video.get("image_paths"):
        try:
            os.remove(video_path)
        except Exception:
            pass

    return tiktok_url, "переозвучено под TikTok" if video.get("image_paths") else "новый текст под TikTok"
```

- [ ] **Step 3: Modify `_finish_publish`**

Find (around lines 345-372):
```python
async def _finish_publish(video_id: str, video: dict, youtube_id: str):
    """После успешной загрузки на YouTube: анонсирует в канале, пробует TikTok,
    и шлёт админу сводку по обеим площадкам. Общий код для первого одобрения
    и для /retry_videos."""
    await announce_in_telegram(youtube_id, video["title"], video.get("thumbnail_path"))

    status_lines = [f"✅ YouTube: https://youtu.be/{youtube_id}"]
    if video["category"] == "catapult":
        block_reason = "продвижение платформы Catapult Trade — TikTok запрещает такой контент"
    else:
        block_reason = await check_tiktok_compliance(_tiktok_caption(video))

    if block_reason:
        status_lines.append(f"⚠️ TikTok пропущен: {block_reason}")
        try:
            os.remove(video["video_path"])
        except Exception:
            pass
    else:
        tiktok_url = await upload_to_tiktok(video["video_path"], _tiktok_caption(video))
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
```
Replace with:
```python
async def _finish_publish(video_id: str, video: dict, youtube_id: str):
    """После успешной загрузки на YouTube: анонсирует в канале, пробует TikTok
    (с одной попыткой более лояльного fallback-варианта при блоке), и шлёт
    админу сводку по обеим площадкам. Общий код для первого одобрения,
    /retry_videos и запланированной публикации."""
    await announce_in_telegram(youtube_id, video["title"], video.get("thumbnail_path"))

    status_lines = [f"✅ YouTube: https://youtu.be/{youtube_id}"]
    block_reason = await check_tiktok_compliance(_tiktok_caption(video))

    if block_reason:
        fallback_result = await _attempt_tiktok_fallback(video_id, video, block_reason)
        if fallback_result:
            tiktok_url, note = fallback_result
            status_lines.append(f"✅ TikTok: {tiktok_url} ({note})")
        else:
            status_lines.append(f"⚠️ TikTok пропущен: {block_reason}")
        try:
            os.remove(video["video_path"])
        except Exception:
            pass
    else:
        tiktok_url = await upload_to_tiktok(video["video_path"], _tiktok_caption(video))
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
```

(The trailing `await notify_admin(...)` call that follows this block — added in Task 3 — is unchanged and still fires after this section.)

- [ ] **Step 4: Syntax check**

Run: `python -m py_compile subagents/yt_publisher.py`
Expected: no output, exit code 0.

- [ ] **Step 5: Commit**

```bash
git add subagents/yt_publisher.py
git commit -m "feat: add one-shot TikTok-safe fallback, drop catapult's hard TikTok skip"
```

---

### Task 11: Wire the new scheduler in `parser.py`

**Files:**
- Modify: `parser.py`

- [ ] **Step 1: Update the `orchestrator` import**

Find (around line 34):
```python
from orchestrator import evening_generation, check_breaking_news, PUBLISH_SCHEDULE, load_poll_state, generate_daily_short, propose_self_record_script, process_self_record_uploads
```
Replace with:
```python
from orchestrator import evening_generation, check_breaking_news, PUBLISH_SCHEDULE, load_poll_state, generate_weekly_batch, propose_self_record_script, process_self_record_uploads
```

- [ ] **Step 2: Update the `yt_publisher` import**

Find (around lines 36-39):
```python
import subagents.yt_publisher as yt_publisher
from subagents.yt_publisher import (
    pending_videos, approved_videos, awaiting_self_record_video, tiktok_retry_pending,
    save_pending_videos, load_pending_videos, handle_video_approval, handle_video_file,
)
```
Replace with:
```python
import subagents.yt_publisher as yt_publisher
from subagents.yt_publisher import (
    pending_videos, approved_videos, awaiting_self_record_video, tiktok_retry_pending,
    save_pending_videos, load_pending_videos, handle_video_approval, handle_video_file,
    publish_due_slot,
)
```

- [ ] **Step 3: Update `cmd_generate_video`**

Find (around lines 110-114):
```python
async def cmd_generate_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TG_ID:
        return
    await update.message.reply_text("🎬 Генерирую YouTube Short...")
    await generate_daily_short()
```
Replace with:
```python
async def cmd_generate_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TG_ID:
        return
    await update.message.reply_text("🎬 Генерирую всю неделю видео (14 штук, это займёт время)...")
    await generate_weekly_batch()
```

- [ ] **Step 4: Replace the daily cron with the weekly-batch cron + 8 publish crons**

Find (around lines 1178-1179):
```python
    # Ежедневная генерация YouTube Short в 21:00 (после вечерней генерации TG-постов в 20:00)
    scheduler.add_job(generate_daily_short, "cron", hour=21, minute=0)
```
Replace with:
```python
    # Еженедельная генерация 14 видео (вс, 19:10 — сразу после контент-плана в 19:00)
    scheduler.add_job(generate_weekly_batch, "cron", day_of_week="sun", hour=19, minute=10)

    # Публикация по расписанию из очереди одобренных видео
    scheduler.add_job(publish_due_slot, "cron", day_of_week="mon,wed,fri", hour=8,  minute=30, args=["forex"])
    scheduler.add_job(publish_due_slot, "cron", day_of_week="mon,wed,fri", hour=19, minute=0,  args=["crypto"])
    scheduler.add_job(publish_due_slot, "cron", day_of_week="tue,thu",     hour=18, minute=30, args=["ai"])
    scheduler.add_job(publish_due_slot, "cron", day_of_week="tue,thu",     hour=20, minute=0,  args=["catapult"])
    scheduler.add_job(publish_due_slot, "cron", day_of_week="sat",         hour=12, minute=30, args=["ai"])
    scheduler.add_job(publish_due_slot, "cron", day_of_week="sat",         hour=14, minute=0,  args=["catapult"])
    scheduler.add_job(publish_due_slot, "cron", day_of_week="sun",         hour=12, minute=30, args=["crypto"])
    scheduler.add_job(publish_due_slot, "cron", day_of_week="sun",         hour=14, minute=0,  args=["ai"])
```

- [ ] **Step 5: Update the startup log line**

Find (around line 1205):
```python
    logger.info("📅 Генерация: каждый день в 20:00")
```
Leave that line as-is (it's about the TG-post pipeline, unrelated to this plan) but add a line after it:
```python
    logger.info("🎬 Генерация видео: воскресенье 19:10 (14 шт/неделю), публикация по расписанию WEEKLY_SCHEDULE")
```

- [ ] **Step 6: Syntax check**

Run:
```bash
python -m py_compile parser.py orchestrator.py subagents/yt_publisher.py subagents/yt_script.py subagents/yt_ideas.py
```
Expected: no output, exit code 0 for each. `py_compile` only parses — it can't catch a wrong import name (e.g. still importing `generate_daily_short`, which no longer exists after Task 6) since that's a runtime lookup, not a syntax error. To actually catch that class of mistake, grep instead of importing (importing `parser.py` locally triggers `os.makedirs("/data/...")` calls from several `subagents/` modules, which behave inconsistently outside a real Railway container and aren't worth chasing here):
```bash
grep -n "generate_daily_short" parser.py orchestrator.py
```
Expected: no matches. If this prints a hit, Step 1 or Step 3 above was missed or mistyped.

- [ ] **Step 7: Commit**

```bash
git add parser.py
git commit -m "feat: wire weekly-batch generation and 8 scheduled publish slots into parser.py"
```

---

### Task 12: Deploy and verify end-to-end

**Files:** none (operational task)

- [ ] **Step 1: Resolve today's leftover pending videos**

In Telegram, approve or cancel (✅/❌ buttons) the 2 pending test videos from earlier `/generate_video` runs today, so production's `pending_videos.json` is empty before the new code (with its new required-by-convention fields) takes over.

- [ ] **Step 2: Push and confirm deploy**

```bash
git push origin main
```
Then poll the Catapult-Bot service's deployment status (Railway MCP or CLI, same approach used earlier this session — `railway deployment list --project 2ffa99f8-afbd-4b52-88cd-23a31a9cb39d --environment 63f574de-9e17-46bd-a5f4-e17f0a576f6f --service 82216bd1-88e4-4581-b1d1-dcc60dc6340d --json`) until the newest deployment's `status` is `SUCCESS`. If `exciting-patience` (legacy service, id `e1d6be12-b4b2-45f4-a9cc-f650a8971cc3`) woke up on this push, stop it (`railway down -y --service e1d6be12-...`), per the project's known quirk.

- [ ] **Step 3: Manual smoke test — one video through the new flow**

In Telegram, send `/generate_video` to the bot. Confirm:
- Bot replies "🎬 Генерирую всю неделю видео (14 штук, это займёт время)..." (not the old single-Short wording).
- Over the following minutes, up to 14 approval messages arrive, each showing a category and (implicitly) built from the broadened topic-candidate pool.
- Tap "✅ Одобрить" on one of them — the message should update to "✅ В очереди. Выйдет: `<день>`, `<время>`" (**not** "⏳ Загружаю на YouTube..." — if you see the old message, Task 8 didn't deploy).
- The video should **not** appear on YouTube/TikTok immediately after approval — it now waits for its scheduled `publish_due_slot` cron tick.

- [ ] **Step 4: Verify a scheduled publish fires**

Either wait for the next real `WEEKLY_SCHEDULE` slot to arrive naturally, or (if immediate confirmation is wanted) manually trigger one category's slot via a Railway shell/SSH one-off: `python -c "import asyncio; from subagents.yt_publisher import publish_due_slot, load_pending_videos; load_pending_videos(); asyncio.run(publish_due_slot('<category of the video approved in Step 3>'))"` from within the running container. Confirm YouTube gets the video and check whether TikTok either published directly or (if the topic trips compliance) the fallback attempt shows up in the admin summary as "✅ TikTok: ... (переозвучено под TikTok)" or "⚠️ TikTok пропущен: ...".

- [ ] **Step 5: Update the plan's checkboxes and note completion**

No commit needed for this task — it's operational, not code.
