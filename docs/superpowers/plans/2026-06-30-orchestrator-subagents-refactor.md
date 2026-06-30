# Orchestrator + Subagents Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the content-pipeline portion of `parser.py` (~1100 of its ~2080 lines) into `orchestrator.py` + `subagents/*.py` modules, with zero behavior change, on branch `refactor/orchestrator-subagents`.

**Architecture:** Pure code relocation. Each task moves a verbatim block of existing functions/constants out of `parser.py` into a new file, then replaces them in `parser.py` with an import. No logic, prompt, or control-flow changes except one explicit, called-out split (`handle_edit_message`).

**Tech Stack:** Python 3.11, python-telegram-bot 20.7, httpx, apscheduler. No new dependencies.

## Global Constraints

- Branch: `refactor/orchestrator-subagents` (already created, already rebased onto latest `main` as of this plan).
- Zero behavior change: every moved function's body must be byte-identical to its current form in `parser.py` — only its file location and the module's imports change.
- `parser.py` on `main` is live in production and changes independently (confirmed: a same-day commit added `"king_ai"` to `CHANNELS["ai"]`). **Before starting Task 1, re-run the sync step below** — do not assume the line numbers in this plan are still exact.
- Do not touch: `bot.py`, the onboarding/quiz/Catapult Connect code in `parser.py` (everything from `WARMUP_SYSTEM_PROMPT` at line 1071 onward), `server.py`.
- Nothing in this plan pushes to `origin` or deploys to Railway. That is a separate, explicit step the user approves after reviewing the full diff.

## Sync step (run once, before Task 1)

- [ ] **Step 1: Re-fetch and rebase**

```bash
cd "/c/Users/Андрей/catapult-bot-git"
git fetch origin
git log origin/main --oneline -3
git diff main origin/main --stat
```

If `origin/main` has new commits, fast-forward `main` and rebase the branch:

```bash
git checkout main && git merge --ff-only origin/main
git checkout refactor/orchestrator-subagents && git rebase main
```

If `parser.py` changed upstream, re-read the affected section with the Read tool before continuing — do not trust this plan's line numbers blindly for that section.

---

### Task 1: `subagents/tg_monitor.py`

**Files:**
- Create: `subagents/tg_monitor.py`
- Modify: `parser.py` (remove lines, add import)
- Test: manual import check (no existing test suite in this repo)

**Interfaces:**
- Produces: `CHANNELS` (dict), `TOP_POSTS` (int), `TGSTAT_TOKEN`, `TGSTAT_API_URL`, `sent_hashes` (set), `make_hash(text: str) -> str`, `get_posts_tgstat(channel: str) -> list`, `get_posts_web(channel: str, hours: int = 24) -> list`, `viral_score(post: dict) -> float`, `collect_top_posts(category: str) -> list`.
- Consumes: nothing from other new modules.

- [ ] **Step 1: Confirm current line numbers**

```bash
grep -n "^def make_hash\|^async def get_posts_tgstat\|^def parse_post_date\|^def parse_post_views\|^async def get_posts_web\|^def viral_score\|^async def collect_top_posts\|^CHANNELS = {\|^TOP_POSTS\|^TGSTAT_TOKEN\|^TGSTAT_API_URL\|^sent_hashes" parser.py
```

Confirm `make_hash` through `collect_top_posts` form one contiguous block (per this plan, lines 176–310 as of the sync step above — i.e. everything from `def make_hash` through the end of `collect_top_posts`, excluding `save_pending`/`save_approved`/`load_pending` which come right before and belong to Task 5). If line numbers shifted, use the grep output instead.

- [ ] **Step 2: Read the exact block to move**

Use the Read tool on `parser.py` with the offset/limit covering `# ── Хэш` through the end of `collect_top_posts` (the blank line before `# ── Claude API`). Verify the content matches what's quoted in Step 3 below — if upstream `parser.py` changed this block, use the freshly-read content instead of what's pasted here.

- [ ] **Step 3: Create `subagents/tg_monitor.py`**

```python
"""
Sub-agent: мониторинг и скоринг постов из Telegram-каналов конкурентов.
Перенесено из parser.py без изменения логики.
"""
import os
import hashlib
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup

TGSTAT_TOKEN     = os.getenv("TGSTAT_TOKEN", "")
TGSTAT_API_URL   = "https://api.tgstat.ru"
TOP_POSTS        = 5

# ── Каналы ────────────────────────────────────────────────────────────────────
CHANNELS = {
    "crypto": [
        "crypto_Iemon", "to_the_makemoney", "airolejon",
        "eeusd", "if_crypto_ru", "cryptomedwed",
        "cryptanci", "DeCenter", "cointelegraph"
    ],
    "ai": [
        "neurobussines", "naebnet", "neyroseti_dr", "loading100ai", "king_ai"
    ],
    "forex": [
        "PROFiInvest", "tradeforexexchange", "premiumgolubev",
        "markoptions", "newwavetrade", "goldenonemoney", "uiartemzvezdin"
    ],
    "catapult": [
        "letsCatapult", "to_the_makemoney", "airolejon", "catapult_community"
    ]
}

# ── Состояние ─────────────────────────────────────────────────────────────────
sent_hashes: set = set()

import logging
logger = logging.getLogger(__name__)

# ── Хэш ───────────────────────────────────────────────────────────────────────
def make_hash(text: str) -> str:
    return hashlib.md5(text[:200].encode()).hexdigest()

# ── TGStat API ────────────────────────────────────────────────────────────────
async def get_posts_tgstat(channel: str) -> list:
    if not TGSTAT_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{TGSTAT_API_URL}/channels/posts",
                params={
                    "token": TGSTAT_TOKEN,
                    "channelId": f"@{channel}",
                    "limit": 10,
                    "extended": 1
                }
            )
            data = resp.json()
            if data.get("status") != "ok":
                return []
            items = data.get("response", {}).get("items", [])
            posts = []
            for item in items:
                text = item.get("text", "").strip()
                views = item.get("viewsCount", 0) or 0
                if len(text) > 100:
                    h = make_hash(text)
                    if h not in sent_hashes:
                        posts.append({"text": text, "channel": channel, "views": views, "hash": h, "source": "tgstat"})
            return posts
    except Exception as e:
        logger.warning(f"TGStat error @{channel}: {e}")
        return []

def parse_post_date(msg) -> datetime | None:
    """Парсим дату поста из HTML"""
    try:
        time_tag = msg.find_parent("div", class_="tgme_widget_message").find("time")
        if time_tag and time_tag.get("datetime"):
            from datetime import timezone
            dt = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        pass
    return None

def parse_post_views(msg) -> int:
    """Парсим просмотры поста"""
    try:
        wrap = msg.find_parent("div", class_="tgme_widget_message")
        views_tag = wrap.find("span", class_="tgme_widget_message_views")
        if views_tag:
            v = views_tag.get_text().strip().replace("K", "000").replace("M", "000000")
            return int("".join(filter(str.isdigit, v)))
    except Exception:
        pass
    return 0

async def get_posts_web(channel: str, hours: int = 24) -> list:
    posts = []
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=15
        ) as client:
            resp = await client.get(f"https://t.me/s/{channel}")
            if resp.status_code != 200:
                return []
            soup = BeautifulSoup(resp.text, "html.parser")
            msgs = soup.find_all("div", class_="tgme_widget_message_text")
            cutoff = datetime.utcnow() - timedelta(hours=hours)

            for msg in msgs:
                text = msg.get_text(separator="\n").strip()
                if len(text) < 100:
                    continue
                h = make_hash(text)
                if h in sent_hashes:
                    continue
                post_date = parse_post_date(msg)
                if post_date and post_date < cutoff:
                    continue
                views = parse_post_views(msg)
                posts.append({
                    "text": text,
                    "channel": channel,
                    "views": views,
                    "hash": h,
                    "source": "web",
                    "date": post_date
                })
    except Exception as e:
        logger.warning(f"Web error @{channel}: {e}")
    return posts

def viral_score(post: dict) -> float:
    """Скор вовлечённости с учётом свежести: просмотры / (часы + 1)"""
    views = post.get("views", 0) or 0
    post_date = post.get("date")
    if post_date:
        age_hours = max(0, (datetime.utcnow() - post_date).total_seconds() / 3600)
    else:
        age_hours = 12
    return views / (age_hours + 1)

async def collect_top_posts(category: str) -> list:
    channels = CHANNELS.get(category, [])
    all_posts = []

    for channel in channels:
        posts = await get_posts_tgstat(channel)
        if not posts:
            posts = await get_posts_web(channel, hours=24)
            if len(posts) < 2:
                posts = await get_posts_web(channel, hours=48)
        all_posts.extend(posts)
        import asyncio
        await asyncio.sleep(0.5)

    all_posts.sort(key=viral_score, reverse=True)
    combined = all_posts[:TOP_POSTS]

    for p in combined:
        score = viral_score(p)
        age = round((datetime.utcnow() - p["date"]).total_seconds() / 3600, 1) if p.get("date") else "?"
        logger.info(f"  [{category}] @{p['channel']} | 👁{p['views']} | ⏱{age}ч | скор={score:.1f}")

    logger.info(f"[{category}] Итого: {len(combined)} постов (из {len(all_posts)})")
    return combined
```

Note: `import asyncio` and `import logging` are placed inline/near-top deliberately matching what's actually used — when you do Step 2's real read, replace the `import asyncio` inside `collect_top_posts` with a top-level `import asyncio` alongside the other imports instead (cleaner; behavior-identical either way since Python caches module imports). Move `import logging` / `logger = logging.getLogger(__name__)` to the top of the file with the other imports.

- [ ] **Step 4: Remove the moved block from `parser.py`, add import**

Delete from `parser.py`: the `CHANNELS = {...}` block, the `sent_hashes: set = set()` line, and everything from `# ── Хэш` through the end of `collect_top_posts`.

Add near the top of `parser.py`, in the imports section:

```python
from subagents.tg_monitor import CHANNELS, sent_hashes, collect_top_posts
```

(`TOP_POSTS`, `TGSTAT_TOKEN`, `make_hash`, `get_posts_tgstat`, `get_posts_web`, `viral_score` are not referenced anywhere else in `parser.py` directly — only through `collect_top_posts` — so they don't need to be re-imported into `parser.py`. Verify this with Step 5 before relying on it.)

- [ ] **Step 5: Verify nothing else in `parser.py` references the removed names directly**

```bash
grep -n "TOP_POSTS\|TGSTAT_TOKEN\|TGSTAT_API_URL\|make_hash\|get_posts_tgstat\|get_posts_web\|viral_score" parser.py
```

Expected: no matches (everything goes through `collect_top_posts` now). If there are matches outside what you just removed, add them to the import line in Step 4.

- [ ] **Step 6: Import sanity check**

```bash
python -c "import subagents.tg_monitor"
```

Expected: no errors (network calls aren't triggered by import alone).

- [ ] **Step 7: Commit**

```bash
git add subagents/tg_monitor.py parser.py
git commit -m "refactor: extract tg_monitor subagent from parser.py"
```

---

### Task 2: `subagents/rewriter.py`

**Files:**
- Create: `subagents/rewriter.py`
- Modify: `parser.py`

**Interfaces:**
- Produces: `CLAUDE_API_KEY`, `CLAUDE_API_URL`, `STYLE_GUIDE`, `CATAPULT_ANGLES`, `generate_post_claude(posts: list, category: str) -> str`, `generate_catapult_post(angle: str) -> str`.
- Consumes: `subagents.tg_monitor.viral_score` (for the news-digest formatting inside `generate_post_claude`).

- [ ] **Step 1: Confirm current line numbers and re-read the block**

```bash
grep -n "^STYLE_GUIDE\|^async def generate_post_claude\|^async def generate_catapult_post\|^CATAPULT_ANGLES = \[\|^CLAUDE_API_KEY\|^CLAUDE_API_URL" parser.py
```

Read the block from `# ── Claude API — генерация поста` through the end of `generate_catapult_post` (just before `# ── ТЗ для картинки`), plus the `CATAPULT_ANGLES` list (currently near the top, alongside `CHANNELS`).

- [ ] **Step 2: Create `subagents/rewriter.py`**

```python
"""
Sub-agent: переделка найденного контента "под свой стиль" через Claude API.
Перенесено из parser.py без изменения логики.
"""
import os
import logging
from datetime import datetime

import httpx

from subagents.tg_monitor import viral_score

logger = logging.getLogger(__name__)

CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_API_URL   = "https://api.anthropic.com/v1/messages"

# ── Углы для Catapult ─────────────────────────────────────────────────────────
CATAPULT_ANGLES = [
    "реферальная программа и заработок на команде",
    "поинты и токены — выгода раннего входа",
    "личный опыт — что я уже накопил и заработал",
    "сравнение с другими платформами — почему Catapult лучше",
    "инструкция как зарегистрироваться и начать",
    "результаты команды — цифры и динамика",
    "ответы на частые вопросы о проекте",
]

# ── Claude API — генерация поста ──────────────────────────────────────────────
STYLE_GUIDE = """Ты — автор Telegram канала «Крипта, AI, Forex. Как заработать?».

Твой стиль:
- Начинаешь с 👋 Друзья, ... или 👋 Друзья, всем привет! или 👋 Друзья, приветствую!
- Каждый абзац начинается с тематического эмодзи
- Пишешь от первого лица, живо и практично
- 150-250 слов
- В конце всегда призыв к действию
- НЕ копируешь дословно — пересказываешь своими словами

ВАЖНО — форматирование ТОЛЬКО через HTML теги Telegram:
- жирный: <b>текст</b>
- курсив: <i>текст</i>
- цитата: <blockquote>текст</blockquote>
- НИКАКИХ звёздочек **текст** — это не работает в Telegram!
- НИКАКОГО markdown форматирования!"""

async def generate_post_claude(posts: list, category: str) -> str:
    context = {
        "crypto": "криптовалюты, Bitcoin, блокчейн, DeFi, альткоины",
        "ai":     "искусственный интеллект, нейросети, AI инструменты для заработка",
        "forex":  "Forex, валютные пары, трейдинг, аналитика рынка",
        "catapult": "торговую платформу Catapult Trade — новости, обновления, партнёрства, акции платформы"
    }

    news_digest = ""
    for i, p in enumerate(posts, 1):
        age = ""
        if p.get("date"):
            age_hours = round((datetime.utcnow() - p["date"]).total_seconds() / 3600, 1)
            age = f"⏱{age_hours}ч назад"
        score = round(viral_score(p), 1)
        news_digest += (
            f"\n--- Новость {i} (@{p['channel']} | 👁{p['views']} просмотров | {age} | скор вирусности={score}) ---\n"
            f"{p['text'][:600]}\n"
        )

    prompt = f"""{STYLE_GUIDE}

Тема: {context.get(category, 'финансы')}

Ниже {len(posts)} свежих постов за последние 24-48 часов из телеграм каналов по теме {category}.
У каждого поста указаны: просмотры, возраст и скор вирусности (просмотры/часы — чем выше, тем горячее).

Выбери САМУЮ горячую и резонансную тему — учитывай скор вирусности и свежесть.
Свежий пост с высоким скором важнее старого с большими просмотрами.
Напиши на её основе один пост для канала с HTML форматированием (теги: <b>, <i>, <blockquote>).
НЕ копируй дословно — осмысли и перескажи своими словами.

{news_digest}

{"В конце добавь: 👉 Подробнее в боте: @catapulttrade_guide_bot" if category == "catapult" else "В конце добавь: 💰 Лучший заработок сегодня здесь: @catapulttrade_guide_bot"}

Только готовый пост, без пояснений."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            data = resp.json()
            if "content" in data:
                return data["content"][0]["text"]
            return text
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return text

# ── Claude API — пост о Catapult ──────────────────────────────────────────────
async def generate_catapult_post(angle: str) -> str:
    prompt = f"""{STYLE_GUIDE}

Напиши пост о торговой платформе Catapult Trade.

Угол: {angle}

Факты о Catapult Trade:
- Торговая платформа где каждая сделка приносит поинты
- Поинты конвертируются в токены платформы при листинге
- Проект на ранней стадии — лучший момент для входа
- Реферальная программа — % от активности команды
- Бот с подробностями: @catapulttrade_guide_bot

Напиши живой пост от первого лица с HTML форматированием.
В конце: 🤖 Все подробности → @catapulttrade_guide_bot

Только готовый пост, без пояснений."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            data = resp.json()
            if "content" in data:
                return data["content"][0]["text"]
            return "Пост о Catapult"
    except Exception as e:
        logger.error(f"Claude Catapult error: {e}")
        return "Пост о Catapult"
```

Note: `generate_post_claude`'s two `return text` lines reference a `text` variable not defined in this function in the *current* code either (pre-existing bug, visible via `grep -n "return text" parser.py` against the function body — it's a leftover from earlier code, only reachable if `"content" not in data`, i.e. Claude API error responses). Preserve as-is — fixing it is out of scope for this zero-behavior-change refactor; flagged here so the reviewer doesn't think it's a transcription error.

- [ ] **Step 3: Remove the moved block from `parser.py`, add import**

Delete the `CATAPULT_ANGLES` list, `STYLE_GUIDE`, `generate_post_claude`, and `generate_catapult_post` from `parser.py`.

Add to `parser.py` imports:

```python
from subagents.rewriter import CATAPULT_ANGLES, generate_post_claude, generate_catapult_post
```

- [ ] **Step 4: Verify no leftover references**

```bash
grep -n "STYLE_GUIDE\|CLAUDE_API_KEY\|CLAUDE_API_URL" parser.py
```

Expected: no matches (only used inside `rewriter.py` now).

- [ ] **Step 5: Import sanity check**

```bash
python -c "import subagents.rewriter"
```

- [ ] **Step 6: Commit**

```bash
git add subagents/rewriter.py parser.py
git commit -m "refactor: extract rewriter subagent from parser.py"
```

---

### Task 3: `subagents/image_brief.py`

**Files:**
- Create: `subagents/image_brief.py`
- Modify: `parser.py`

**Interfaces:**
- Produces: `generate_image_brief(post_text: str, category: str) -> str`.

- [ ] **Step 1: Confirm current line numbers**

```bash
grep -n "^async def generate_image_brief" parser.py
```

- [ ] **Step 2: Create `subagents/image_brief.py`**

```python
"""
Sub-agent: ТЗ для картинки на основе текста поста.
Перенесено из parser.py без изменения логики.
"""
import logging

import httpx

from subagents.rewriter import CLAUDE_API_KEY, CLAUDE_API_URL

logger = logging.getLogger(__name__)

# ── ТЗ для картинки ───────────────────────────────────────────────────────────
async def generate_image_brief(post_text: str, category: str) -> str:
    category_style = {
        "crypto":   "тёмный фон, неоновые синие и оранжевые цвета, Bitcoin/крипто символика, торговые графики",
        "ai":       "тёмный фон, фиолетовые и голубые цвета, нейронные сети, цифровые паттерны",
        "forex":    "тёмный фон, зелёные и синие цвета, валютные пары, торговые графики",
        "catapult": "тёмный фон, золотые и оранжевые цвета, ракета/запуск, трейдинг платформа",
    }
    style = category_style.get(category, category_style["crypto"])
    prompt = f"""На основе этого поста составь короткое ТЗ для дизайнера/Midjourney на создание картинки.

Пост:
{post_text[:500]}

Стиль: {style}, размер 1200x630px, кинематографично, фотореалистично.

Напиши ТЗ в 2-3 предложения: что должно быть на картинке, цвета, настроение.
Пиши простым текстом без markdown, без звёздочек, без заголовков. Только текст."""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            data = resp.json()
            if "content" in data:
                return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Brief error: {e}")
    return f"Фотореалистичная картинка на тему {category}, тёмный фон, неоновые цвета, 1200x630px."
```

- [ ] **Step 3: Remove from `parser.py`, add import**

Delete `generate_image_brief` from `parser.py`. Add:

```python
from subagents.image_brief import generate_image_brief
```

- [ ] **Step 4: Import sanity check**

```bash
python -c "import subagents.image_brief"
```

- [ ] **Step 5: Commit**

```bash
git add subagents/image_brief.py parser.py
git commit -m "refactor: extract image_brief subagent from parser.py"
```

---

### Task 4: `subagents/weekly_plan.py`

**Files:**
- Create: `subagents/weekly_plan.py`
- Modify: `parser.py`

**Interfaces:**
- Produces: `generate_weekly_plan() -> str`, `send_weekly_plan() -> None`.
- Consumes: `parser.PARSER_BOT_TOKEN`, `parser.ADMIN_TG_ID` (passed as arguments, not imported — see Step 2 note).

- [ ] **Step 1: Confirm current line numbers**

```bash
grep -n "^async def generate_weekly_plan\|^async def send_weekly_plan" parser.py
```

- [ ] **Step 2: Read both functions, then create `subagents/weekly_plan.py`**

`send_weekly_plan` in the current code reads module-level `PARSER_BOT_TOKEN` and `ADMIN_TG_ID` directly (no parameters). Since those constants stay defined in `parser.py` (they're bot-wiring config, not pipeline state), give `send_weekly_plan` two parameters instead of relying on globals — this is the one place besides `handle_edit_message` where the shape changes slightly, because Python module globals don't cross files. Document this clearly:

```python
"""
Sub-agent: воскресный контент-план.
Перенесено из parser.py. Note: send_weekly_plan takes bot_token/admin_id as
parameters instead of reading module globals, since those now live in parser.py.
"""
import logging

import httpx

from subagents.rewriter import CLAUDE_API_KEY, CLAUDE_API_URL

logger = logging.getLogger(__name__)

# ── Воскресный контент-план ───────────────────────────────────────────────────
async def generate_weekly_plan() -> str:
    prompt = f"""Составь контент-план на неделю для Telegram канала «Крипта, AI, Forex. Как заработать?».

Расписание каждого дня:
09:00 — Крипта
11:00 — Catapult Trade
13:00 — ИИ
15:00 — Catapult Trade
16:30 — Опрос
18:00 — Форекс
20:00 — Крипта

Напиши план на 7 дней (Пн-Вс). Для каждого поста укажи конкретную тему/идею.
Формат: день → время → тема одной строкой.
Используй эмодзи. Без лишних слов."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1500,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            data = resp.json()
            if "content" in data:
                return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Weekly plan error: {e}")
    return "⚠️ Не удалось сгенерировать контент-план."

async def send_weekly_plan(bot_token: str, admin_id: int):
    logger.info("=== Воскресный контент-план ===")
    plan = await generate_weekly_plan()
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": admin_id,
                "text": f"📅 <b>КОНТЕНТ-ПЛАН НА НЕДЕЛЮ</b>\n\n{plan}",
                "parse_mode": "HTML"
            }
        )
```

- [ ] **Step 3: Remove from `parser.py`, add import, update the one call site**

Delete `generate_weekly_plan` and `send_weekly_plan` from `parser.py`. Add:

```python
from subagents.weekly_plan import send_weekly_plan
```

Find the scheduler line that registers the Sunday job (in `main()`):

```bash
grep -n "send_weekly_plan" parser.py
```

It currently reads `scheduler.add_job(send_weekly_plan, "cron", day_of_week="sun", hour=19, minute=0)`. Change it to pass the now-required arguments via a lambda, matching how `evening_generation`'s job is already wired in this file (check `scheduler.add_job(evening_generation, ...)` — if Task 6 already changed that pattern, match it; otherwise use):

```python
scheduler.add_job(
    lambda: app.create_task(send_weekly_plan(PARSER_BOT_TOKEN, ADMIN_TG_ID)),
    "cron", day_of_week="sun", hour=19, minute=0
)
```

Confirm `PARSER_BOT_TOKEN` and `ADMIN_TG_ID` are still defined at module level in `parser.py` at this point (they are — they're bot-wiring config, never moved).

- [ ] **Step 4: Import sanity check**

```bash
python -c "import subagents.weekly_plan"
```

- [ ] **Step 5: Commit**

```bash
git add subagents/weekly_plan.py parser.py
git commit -m "refactor: extract weekly_plan subagent from parser.py"
```

---

### Task 5: `subagents/tg_publisher.py`

**Files:**
- Create: `subagents/tg_publisher.py`
- Modify: `parser.py`

**Interfaces:**
- Produces: `CHANNEL_SIGNATURE`, `PHOTOS_DIR`, `PENDING_FILE`, `APPROVED_FILE`, `pending_posts` (dict), `approved_queue` (dict), `awaiting_photo` (dict), `save_pending()`, `save_approved()`, `load_pending()`, `approval_keyboard(post_id: str) -> dict`, `send_for_approval(...)`, `handle_approval(update, context)`, `handle_photo(update, context)`, `auto_publish(slot: str)`, `handle_admin_edit(update, context)` (new — see Step 2).
- Consumes: `subagents.image_brief.generate_image_brief`, `subagents.rewriter.{generate_catapult_post, generate_post_claude, CATAPULT_ANGLES}`.

This is the largest extraction. Do it in two reads.

- [ ] **Step 1: Confirm current line numbers**

```bash
grep -n "^def save_pending\|^def save_approved\|^def load_pending\|^def approval_keyboard\|^async def send_for_approval\|^async def handle_approval\|^async def handle_photo\|^async def handle_edit_message\|^async def auto_publish\|^PENDING_FILE\|^APPROVED_FILE\|^PHOTOS_DIR\|^CHANNEL_SIGNATURE" parser.py
```

- [ ] **Step 2: Read `handle_edit_message`'s current full body before extracting anything**

This function currently dispatches on `user_id == ADMIN_TG_ID`. The non-admin branch (calling `handle_legacy_text`, `handle_api_key_for_users`, `handle_warmup_message`) stays in `parser.py` untouched. The admin branch — everything from `if update.message.text == "/cancel":` through the final `client.post(...)` sendMessage call — becomes `handle_admin_edit` in `tg_publisher.py`. Read the exact current body with the Read tool before transcribing, since this is the one function in this refactor that gets split rather than moved whole.

- [ ] **Step 3: Create `subagents/tg_publisher.py`**

```python
"""
Sub-agent: approval-цикл и публикация в Telegram-канал.
Перенесено из parser.py. handle_admin_edit is new — it's the admin-only
body that used to be inlined inside parser.py's handle_edit_message
dispatcher; the dispatcher itself (and its non-admin branch) stays in
parser.py. No logic changed, only relocated.
"""
import os
import json
import random
import logging

import httpx
from telegram import Update, InputFile
from telegram.ext import ContextTypes

from subagents.image_brief import generate_image_brief
from subagents.rewriter import generate_catapult_post, generate_post_claude, CATAPULT_ANGLES

logger = logging.getLogger(__name__)

# ── Подпись канала ────────────────────────────────────────────────────────────
CHANNEL_SIGNATURE = """

———
🔔 Подпишись и не пропусти важное

▶️ <a href="#">YouTube</a> | 💬 <a href="https://t.me/Crypto_AI_Forex_Chat">TG Chat</a> | 🎵 <a href="#">TikTok</a> | 📷 <a href="#">Instagram</a> | 🤖 <a href="https://t.me/catapulttrade_guide_bot">TG Bot</a> | 🐦 <a href="#">Twitter</a>"""

# ── Состояние ─────────────────────────────────────────────────────────────────
pending_posts: dict = {}
approved_queue: dict = {}
awaiting_photo: dict = {}

PENDING_FILE  = "/app/pending_posts.json"
APPROVED_FILE = "/app/approved_queue.json"
PHOTOS_DIR    = "/app/photos"
os.makedirs(PHOTOS_DIR, exist_ok=True)

def save_pending():
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(pending_posts, f, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"Save pending error: {e}")

def save_approved():
    try:
        with open(APPROVED_FILE, "w", encoding="utf-8") as f:
            json.dump(approved_queue, f, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"Save approved error: {e}")

def load_pending():
    global pending_posts, approved_queue
    try:
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                pending_posts = json.load(f)
            logger.info(f"Загружено {len(pending_posts)} pending постов")
    except Exception as e:
        logger.error(f"Load pending error: {e}")
    try:
        if os.path.exists(APPROVED_FILE):
            with open(APPROVED_FILE, "r", encoding="utf-8") as f:
                approved_queue.update(json.load(f))
            logger.info(f"Загружено {len(approved_queue)} approved постов")
    except Exception as e:
        logger.error(f"Load approved error: {e}")

# ── Клавиатура одобрения ──────────────────────────────────────────────────────
def approval_keyboard(post_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Одобрить",      "callback_data": f"approve_{post_id}"},
            {"text": "🔄 Переписать",    "callback_data": f"rewrite_{post_id}"},
        ], [
            {"text": "✏️ Редактировать", "callback_data": f"edit_{post_id}"},
            {"text": "❌ Отменить",      "callback_data": f"cancel_{post_id}"},
        ]]
    }
```

Continue the same file with `send_for_approval`, `handle_approval`, `handle_photo`, `handle_admin_edit`, `auto_publish` — read each verbatim from the current `parser.py` per Steps 1-2 and append in the same order they appear today. They reference module-level `PARSER_BOT_TOKEN`, `ADMIN_TG_ID`, `MAIN_BOT_TOKEN`, `CHANNEL_ID`, `editing_post` — these stay defined in `parser.py`; **add `from parser import PARSER_BOT_TOKEN, ADMIN_TG_ID, MAIN_BOT_TOKEN, CHANNEL_ID, editing_post` is wrong (circular import — `parser.py` will import from `tg_publisher`)**. Instead, since these are read-only lookups of simple config values that `parser.py` already owns, pass them as function parameters or read them via a small `config` shim:

Use this pattern instead — add a `set_config(...)` function so `parser.py` injects its own already-defined constants once at startup, avoiding the circular import:

```python
PARSER_BOT_TOKEN = None
ADMIN_TG_ID = None
MAIN_BOT_TOKEN = None
CHANNEL_ID = None
editing_post: dict = {}

def configure(parser_bot_token: str, admin_tg_id: int, main_bot_token: str, channel_id: str):
    global PARSER_BOT_TOKEN, ADMIN_TG_ID, MAIN_BOT_TOKEN, CHANNEL_ID
    PARSER_BOT_TOKEN = parser_bot_token
    ADMIN_TG_ID = admin_tg_id
    MAIN_BOT_TOKEN = main_bot_token
    CHANNEL_ID = channel_id
```

Place this `configure()` function and the four `None`-initialized globals right after the `PENDING_FILE`/`APPROVED_FILE`/`PHOTOS_DIR` block, before `save_pending`. Then in `parser.py`'s `main()`, before constructing the bot apps, add one call:

```python
tg_publisher.configure(PARSER_BOT_TOKEN, ADMIN_TG_ID, MAIN_BOT_TOKEN, CHANNEL_ID)
```

(`editing_post` is only ever read/written inside `tg_publisher.py`'s `handle_admin_edit`/`handle_approval`/the `/cancel` path now — confirm with `grep -n "editing_post" parser.py` after Task 5's removal that nothing outside `tg_publisher.py` still touches it. If something does, export it the same way as `pending_posts`/`approved_queue`/`awaiting_photo` via a plain module-level dict.)

Append `send_for_approval`, `handle_approval`, `handle_photo`, `auto_publish` to `tg_publisher.py`: read each one verbatim from the current `parser.py` with the Read tool, using the line numbers from Step 1, and paste it into `tg_publisher.py` with **zero text changes** — every reference to `PARSER_BOT_TOKEN`, `ADMIN_TG_ID`, `MAIN_BOT_TOKEN`, `CHANNEL_ID`, `pending_posts`, `approved_queue`, `awaiting_photo` inside these bodies already matches the names now defined at the top of this module (via `configure()` and the module-level dicts), so no find-and-replace is needed inside the function bodies. Preserve the original order: `send_for_approval`, then `handle_approval`, then `handle_photo`, then `auto_publish`.

Add `handle_admin_edit` as a new function, built from the admin branch of the current `handle_edit_message` (everything after `if user_id != ADMIN_TG_ID: ... return` in the original — i.e., starting from `if update.message.text == "/cancel":`):

```python
async def handle_admin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if update.message.text == "/cancel":
        editing_post.pop(user_id, None)
        awaiting_photo.pop(user_id, None)
        await update.message.reply_text("✅ Отменено.")
        return

    post_id = editing_post.get(user_id)
    if not post_id:
        return

    post = pending_posts.get(post_id)
    if not post:
        await update.message.reply_text("⚠️ Пост не найден.")
        editing_post.pop(user_id, None)
        return

    new_text = update.message.text
    pending_posts[post_id]["text"] = new_text + CHANNEL_SIGNATURE
    editing_post.pop(user_id, None)

    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": (
                    f"✅ <b>Текст обновлён!</b>\n\n"
                    f"{new_text[:600]}\n\n"
                    f"🖼 <b>ТЗ для картинки:</b>\n{post['brief']}"
                ),
                "parse_mode": "HTML",
                "reply_markup": approval_keyboard(post_id)
            }
        )
```

- [ ] **Step 4: Update `parser.py`'s `handle_edit_message` to a thin dispatcher**

Replace the current `handle_edit_message` body with:

```python
async def handle_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id != ADMIN_TG_ID:
        handled = await handle_legacy_text(update, context)
        if handled:
            return
        if context.user_data.get('awaiting_api_key'):
            await handle_api_key_for_users(update, context)
        else:
            await handle_warmup_message(update, context)
        return

    await tg_publisher.handle_admin_edit(update, context)
```

(Keep the existing comment above it from the original, if any, describing this dual role — copy it verbatim rather than rewriting.)

- [ ] **Step 5: Remove the rest of the moved code from `parser.py`, add imports**

Delete from `parser.py`: `CHANNEL_SIGNATURE`, the `pending_posts`/`approved_queue`/`awaiting_photo` globals, `PENDING_FILE`/`APPROVED_FILE`/`PHOTOS_DIR` + `os.makedirs`, `save_pending`, `save_approved`, `load_pending`, `approval_keyboard`, `send_for_approval`, `handle_approval`, `handle_photo`, `auto_publish`, and the admin-branch body that's now `handle_admin_edit`.

Add to `parser.py` imports:

```python
import subagents.tg_publisher as tg_publisher
from subagents.tg_publisher import (
    pending_posts, approved_queue, awaiting_photo, editing_post,
    save_pending, load_pending, handle_approval, handle_photo,
    auto_publish, send_for_approval,
)
```

- [ ] **Step 6: Wire `tg_publisher.configure(...)` into `main()`**

Find `main()` in `parser.py` and add the configure call before the two `Application.builder()` calls:

```bash
grep -n "async def main\|Application.builder" parser.py
```

- [ ] **Step 7: Verify every call site still matches**

```bash
grep -n "pending_posts\|approved_queue\|awaiting_photo\|editing_post\|save_pending\|save_approved\|load_pending\|CHANNEL_SIGNATURE" parser.py
```

Every remaining reference in `parser.py` should be either the import line itself or a usage that's valid against the imported names (e.g. `cmd_queue` reads `approved_queue`/`pending_posts` — those still resolve correctly since they're imported).

- [ ] **Step 8: Import sanity check**

```bash
python -c "import subagents.tg_publisher"
```

- [ ] **Step 9: Commit**

```bash
git add subagents/tg_publisher.py parser.py
git commit -m "refactor: extract tg_publisher subagent from parser.py, split handle_edit_message"
```

---

### Task 6: `orchestrator.py`

**Files:**
- Create: `orchestrator.py`
- Modify: `parser.py`

**Interfaces:**
- Produces: `evening_generation()`, `PUBLISH_SCHEDULE` (list).
- Consumes: `subagents.tg_monitor.collect_top_posts`, `subagents.rewriter.{generate_post_claude, generate_catapult_post, CATAPULT_ANGLES}`, `subagents.tg_publisher.send_for_approval`.

- [ ] **Step 1: Confirm current line numbers**

```bash
grep -n "^async def evening_generation\|^PUBLISH_SCHEDULE\|^POLL_TOPICS\|catapult_angle_idx\|poll_idx\|last_poll_date" parser.py
```

- [ ] **Step 2: Read `evening_generation`'s current full body** (lines ~802-910 as of the sync step) plus `PUBLISH_SCHEDULE` and `POLL_TOPICS` (currently near the top, alongside `CHANNELS`/`CATAPULT_ANGLES`) and the three module-level state vars `catapult_angle_idx`, `poll_idx`, `last_poll_date`.

- [ ] **Step 3: Create `orchestrator.py`**

```python
"""
Дирижёр (orchestrator): связывает мониторинг -> rewrite -> image-brief ->
approval для контент-пайплайна Telegram. Перенесено из parser.py (бывшая
evening_generation) без изменения логики.
"""
import hashlib
import logging

from subagents.tg_monitor import collect_top_posts, sent_hashes
from subagents.rewriter import generate_post_claude, generate_catapult_post, CATAPULT_ANGLES
from subagents.tg_publisher import send_for_approval, PARSER_BOT_TOKEN, ADMIN_TG_ID

import httpx

logger = logging.getLogger(__name__)

# ── Расписание публикаций (следующий день) ────────────────────────────────────
PUBLISH_SCHEDULE = [
    {"hour": 9,  "minute": 0,  "slot": "crypto_1"},
    {"hour": 11, "minute": 0,  "slot": "catapult_1"},
    {"hour": 13, "minute": 0,  "slot": "ai"},
    {"hour": 16, "minute": 30, "slot": "poll"},
    {"hour": 18, "minute": 0,  "slot": "forex"},
    {"hour": 20, "minute": 0,  "slot": "crypto_2"},
]

# ── Темы опросов ──────────────────────────────────────────────────────────────
POLL_TOPICS = [
    {
        "question": "💰 Во что инвестируешь прямо сейчас?",
        "options": ["BTC/ETH", "Альткоины", "Форекс", "Ничего, жду"]
    },
    {
        "question": "📈 Какой твой горизонт инвестиций?",
        "options": ["До 1 месяца", "1–6 месяцев", "1+ год", "Я спекулянт"]
    },
    {
        "question": "🤖 Используешь ли AI в трейдинге?",
        "options": ["Да, активно", "Иногда пробую", "Нет", "Хочу начать"]
    },
    {
        "question": "🆘 Что мешает начать торговать?",
        "options": ["Нет знаний", "Нет стартового капитала", "Боюсь рисков", "Уже торгую!"]
    },
    {
        "question": "🏆 Какой рынок сейчас интереснее?",
        "options": ["Крипта 🪙", "Форекс 💹", "Акции 📊", "Всё интересно"]
    },
    {
        "question": "🎯 Торгуешь по стратегии или интуитивно?",
        "options": ["Строго по стратегии", "В основном интуиция", "Микс", "Только учусь"]
    },
    {
        "question": "⏰ Как часто проверяешь рынок?",
        "options": ["Каждый час", "Раз в день", "Раз в неделю", "Постоянно слежу"]
    },
]

# ── Состояние ─────────────────────────────────────────────────────────────────
catapult_angle_idx: int = 0
poll_idx: int = 0
last_poll_date: str = ""
```

Append `evening_generation` to this file, copied verbatim from the current `parser.py` (read it with the Read tool using the line range from Step 1) with **zero text changes**, including its inline poll-construction block that writes `pending_posts[poll_id] = {...}` directly. Add this import alongside the others at the top of `orchestrator.py`:

```python
from subagents.tg_publisher import pending_posts, send_for_approval, PARSER_BOT_TOKEN, ADMIN_TG_ID
```

This is safe without any code change inside `evening_generation`: `pending_posts` is a `dict`, a mutable object — `from module import pending_posts` binds a reference to the *same* dict object `tg_publisher.py` holds, so `pending_posts[poll_id] = {...}` inside `evening_generation` mutates the one shared dict, visible from both modules.

- [ ] **Step 4: Remove the moved code from `parser.py`, add imports**

Delete from `parser.py`: `PUBLISH_SCHEDULE`, `POLL_TOPICS`, `catapult_angle_idx`/`poll_idx`/`last_poll_date` globals, and `evening_generation`.

Add to `parser.py` imports:

```python
from orchestrator import evening_generation, PUBLISH_SCHEDULE
```

- [ ] **Step 5: Verify the scheduler wiring in `main()` still matches**

```bash
grep -n "evening_generation\|PUBLISH_SCHEDULE\|auto_publish" parser.py
```

`main()`'s `scheduler.add_job(evening_generation, "cron", hour=20, minute=0)` and the `for s in PUBLISH_SCHEDULE: scheduler.add_job(auto_publish, ...)` loop should both still resolve correctly via the new imports — confirm by reading, don't just assume.

- [ ] **Step 6: Import sanity check**

```bash
python -c "import orchestrator"
```

- [ ] **Step 7: Commit**

```bash
git add orchestrator.py parser.py
git commit -m "refactor: extract orchestrator (evening_generation) from parser.py"
```

---

### Task 7: Full verification pass

**Files:** none created; read-only verification across the branch.

- [ ] **Step 1: Confirm `parser.py` only contains what the spec says it should**

```bash
grep -n "^async def \|^def \|^class " parser.py
```

Expected remaining function definitions in `parser.py`: bot command handlers (`cmd_scan`/`cmd_status`-equivalents if any, `cmd_generate`, `cmd_cancel`, `cmd_test_generate`, `cmd_test_publish`, `cmd_queue`), `handle_edit_message` (now the thin dispatcher), `main()`, and everything from `claude_warmup_reply` onward (onboarding/quiz/Catapult Connect — untouched, out of scope).

- [ ] **Step 2: Full diff review against `main`**

```bash
git diff main --stat
git diff main -- parser.py
```

Read the full `parser.py` diff. Confirm every removed line corresponds to code that now exists verbatim in one of the five new files (spot-check at least the Claude prompts and the `CHANNELS`/`PUBLISH_SCHEDULE` dicts character-for-character, since these are the easiest to silently corrupt during a manual copy).

- [ ] **Step 3: Compile-check every changed file**

```bash
python -m py_compile parser.py orchestrator.py subagents/tg_monitor.py subagents/rewriter.py subagents/image_brief.py subagents/tg_publisher.py subagents/weekly_plan.py
```

Expected: no output, exit code 0.

- [ ] **Step 4: Full import of the entrypoint**

```bash
python -c "import parser"
```

Expected: no errors. This will fail loudly if any moved name wasn't re-exported/imported correctly — that's the point of this check.

- [ ] **Step 5: Report to the user**

Summarize: which files were created, the full `git diff main --stat`, and explicitly flag the two non-mechanical spots a human should double check (the `handle_edit_message` split in Task 5, and the `send_weekly_plan` parameter change in Task 4). Do not push or deploy — wait for explicit user go-ahead per the spec's safety plan.

---

## Out of scope (do not do in this plan)

- Pushing `refactor/orchestrator-subagents` to `origin` or merging to `main`.
- Deploying to Railway.
- Any change to `bot.py`, `server.py`, or the onboarding/quiz/Catapult Connect code in `parser.py`.
- Fixing the pre-existing `return text` bug noted in Task 2, the `catapult_2`/`PUBLISH_SCHEDULE` mismatch noted earlier in this project, or adding the Instagram/Twitter/YouTube/TikTok subagents (separate future plan).
