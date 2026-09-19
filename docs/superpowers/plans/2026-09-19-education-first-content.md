# Обучающий контент вместо новостного пересказа — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перевести запланированные посты, картинки и видео рубрик crypto/forex/ai с новостного пересказа на последовательные обучающие уроки, отключить Catapult, сохранить горячие новости.

**Architecture:** Банк уроков (`edu_curriculum.py`) + прогресс в `/data/edu_progress.json` (`edu_progress.py`): один урок на рубрику в день, пост/второй пост/видео этого дня берут его с разных сторон. Новые генераторы урока (пост, ТЗ картинки, сценарий видео) вызываются из `orchestrator.evening_generation` и `_generate_and_queue_video`. Часовые горячие новости не меняются.

**Tech Stack:** Python 3.11, httpx, Claude API (`claude-sonnet-4-6`), Gemini image API, Railway (`/data` том). Автотестов в репозитории нет — проверка `py_compile`, короткие проверочные скрипты и пробный прогон с превью админу без публикации (принятая практика проекта).

**Spec:** `docs/superpowers/specs/2026-09-19-education-first-content-design.md`

## Global Constraints

- Тексты постов — Telegram HTML только `<b> <i> <blockquote> <a href>`, без markdown (правило `STYLE_GUIDE`).
- Во всех промптах генерации публикуемого текста присутствует `NEUTRALITY_NOTE` (нет российских реалий).
- Уроки не обещают гарантированной доходности и не являются персональным финансовым советом.
- В forex-контенте слово «бот» не используется — «софт/программа/система» (для forex-видео призыв к автотрейдингу и `FOREX_AUTOTRADE_OFFER` сохраняются).
- Идентификатор слота `catapult_1` не переименовывать; функции/стили Catapult не удалять.
- Пуш в `main` запускает деплой и оживляет `exciting-patience` — пушить только в Task 7, после подтверждения превью пользователем.
- Секреты не печатать.

---

### Task 1: Банк уроков — ВЫПОЛНЕНО при составлении плана

**Files:**
- Create: `subagents/edu_curriculum.py` (готов: `CURRICULUM = {"crypto"|"forex"|"ai": [ {id, level, title, points} × 40 ]}`, `LEVEL_NAMES`)

**Interfaces:**
- Produces: `CURRICULUM: dict[str, list[dict]]`, `LEVEL_NAMES: dict[int, str]`

- [x] Файл создан, проверено: по 40 уроков, уникальные id, уровни по возрастанию.

---

### Task 2: Прогресс уроков

**Files:**
- Create: `subagents/edu_progress.py`

**Interfaces:**
- Consumes: `CURRICULUM` (Task 1); `subagents.yt_script._call_claude(prompt: str, max_tokens: int) -> str | None`
- Produces:
  - `peek_or_assign(category: str, day: str, bonus: bool = False, path: str = PROGRESS_FILE) -> dict | None` — урок `{id, level, title, points, number}` (`number` — 1-based) или `None`, если банк кончился
  - `async get_lesson(category: str, day: str, bonus: bool = False, path: str = PROGRESS_FILE) -> dict | None` — то же, но при исчерпании банка достраивает его через Claude

- [ ] **Step 1: Создать модуль**

```python
"""
Прогресс обучающей программы. Один урок на рубрику в день: пост, второй пост
и видео этого дня получают ОДИН И ТОТ ЖЕ урок (день = дата публикации,
YYYY-MM-DD). bonus=True — отдельный дополнительный урок в тот же день
(forex-пост 11:00 по вт/пт). Состояние переживает рестарты (/data).
"""
import json
import logging
import os

from subagents.edu_curriculum import CURRICULUM

logger = logging.getLogger(__name__)

PROGRESS_FILE = "/data/edu_progress.json"
EXTEND_COUNT = 5


def _load(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
                if isinstance(state, dict):
                    return state
    except Exception as e:
        logger.error(f"edu_progress load error: {e}")
    return {}


def _save(state: dict, path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"edu_progress save error: {e}")


def _bank(state: dict, category: str) -> list:
    return CURRICULUM[category] + state.get("extra", {}).get(category, [])


def peek_or_assign(category: str, day: str, bonus: bool = False, path: str = PROGRESS_FILE) -> dict | None:
    state = _load(path)
    bank = _bank(state, category)
    assigned = state.setdefault("assigned", {}).setdefault(category, {})
    key = f"{day}:bonus" if bonus else day
    if key in assigned:
        idx = assigned[key]
    else:
        idx = state.setdefault("next", {}).get(category, 0)
        if idx >= len(bank):
            return None
        assigned[key] = idx
        state["next"][category] = idx + 1
        _save(state, path)
    return {**bank[idx], "number": idx + 1}


async def extend_bank(category: str, path: str = PROGRESS_FILE) -> bool:
    """Банк исчерпан — просим Claude придумать следующие уроки того же уровня."""
    from subagents.yt_script import _call_claude
    state = _load(path)
    bank = _bank(state, category)
    done_titles = "\n".join(f"- {l['title']}" for l in bank[-30:])
    raw = await _call_claude(
        f"""Ты методист обучающего канала по теме «{category}» (крипта/forex/ИИ).
Уже пройденные уроки (последние):
{done_titles}

Придумай {EXTEND_COUNT} СЛЕДУЮЩИХ уроков продвинутого уровня, не повторяя пройденные.
Ответь СТРОГО строками формата: заголовок | что раскрыть (одно предложение). Без нумерации и пояснений.""",
        max_tokens=600,
    )
    if not raw:
        return False
    extra = state.setdefault("extra", {}).setdefault(category, [])
    added = 0
    for line in raw.splitlines():
        if "|" not in line:
            continue
        title, points = (p.strip(" -•\t") for p in line.split("|", 1))
        if title and points:
            extra.append({"id": f"{category}x{len(extra) + 1}", "level": 3, "title": title, "points": points})
            added += 1
    if added:
        _save(state, path)
    return added > 0


async def get_lesson(category: str, day: str, bonus: bool = False, path: str = PROGRESS_FILE) -> dict | None:
    lesson = peek_or_assign(category, day, bonus, path)
    if lesson is None and await extend_bank(category, path):
        lesson = peek_or_assign(category, day, bonus, path)
    return lesson
```

- [ ] **Step 2: Проверить логику на временном файле**

```bash
cd /c/Users/Андрей/catapult-bot-git && python - <<'PY'
import os, tempfile
from subagents.edu_progress import peek_or_assign
p = os.path.join(tempfile.mkdtemp(), "p.json")
a = peek_or_assign("crypto", "2026-09-20", path=p)
b = peek_or_assign("crypto", "2026-09-20", path=p)          # тот же день -> тот же урок
c = peek_or_assign("crypto", "2026-09-21", path=p)          # следующий день -> следующий
d = peek_or_assign("crypto", "2026-09-21", bonus=True, path=p)
assert a["id"] == b["id"] == "c01" and a["number"] == 1
assert c["id"] == "c02" and d["id"] == "c03"
assert peek_or_assign("forex", "2026-09-20", path=p)["id"] == "f01"   # рубрики независимы
for i in range(60):                                          # исчерпание банка -> None
    last = peek_or_assign("ai", f"2027-01-{i:02d}", path=p)
assert last is None
print("edu_progress OK")
PY
```
Expected: `edu_progress OK`

- [ ] **Step 3: Commit**

```bash
git add subagents/edu_curriculum.py subagents/edu_progress.py
git commit -m "feat: банк обучающих уроков и прогресс по дням"
```

---

### Task 3: Генераторы урока — пост и ТЗ картинки

**Files:**
- Modify: `subagents/rewriter.py` (добавить после `generate_post_claude`, перед `generate_catapult_post`; импорт `LEVEL_NAMES`)
- Modify: `subagents/image_brief.py` (добавить после `generate_image_brief`)

**Interfaces:**
- Consumes: урок `{id, level, title, points, number}` (Task 2)
- Produces:
  - `async generate_lesson_post(lesson: dict, category: str, role: str) -> str` — `role` ∈ `"lesson" | "practice"`; пустая строка при сбое Claude (как `generate_post_claude`, ошибка пишется в `LAST_CLAUDE_ERROR`)
  - `async generate_lesson_image_brief(lesson: dict, category: str) -> str`

- [ ] **Step 1: `rewriter.py` — импорт и константы**

После строки `from subagents.tg_monitor import viral_score` добавить:

```python
from subagents.edu_curriculum import LEVEL_NAMES
```

После определения `STYLE_GUIDE` (перед `async def generate_post_claude`) добавить:

```python
LESSON_CONTEXT = {
    "crypto": "криптовалюты и трейдинг на криптобиржах",
    "forex":  "Forex и технический анализ валютных пар",
    "ai":     "искусственный интеллект и заработок/автоматизация с его помощью",
}

LESSON_ROLE_INSTRUCTION = {
    "lesson": (
        "Полноценный урок: хук-вопрос из жизни новичка → объяснение простыми словами → "
        "конкретный пример с цифрами → 3-5 шагов или чек-лист → одна типичная ошибка → "
        "тизер следующего урока."
    ),
    "practice": (
        "Закрепление ТОГО ЖЕ урока, без повторного объяснения с нуля: 3-5 типичных ошибок новичков "
        "по теме и как их избежать, затем мини-квиз из 2 вопросов (ответы дай отдельной строкой в конце)."
    ),
}
```

- [ ] **Step 2: `rewriter.py` — функция `generate_lesson_post`** (вставить перед `# ── Claude API — пост о Catapult`)

```python
# ── Claude API — обучающий пост-урок ──────────────────────────────────────────
async def generate_lesson_post(lesson: dict, category: str, role: str = "lesson") -> str:
    prompt = f"""{STYLE_GUIDE}

Это ОБУЧАЮЩИЙ пост для рубрики: {LESSON_CONTEXT.get(category, 'финансы')}.
Урок №{lesson['number']}: {lesson['title']}
Уровень: {LEVEL_NAMES.get(lesson['level'], '')}
Что раскрыть: {lesson['points']}

Формат: {LESSON_ROLE_INSTRUCTION.get(role, LESSON_ROLE_INSTRUCTION['lesson'])}
После приветствия — строка заголовка: 📚 <b>Урок {lesson['number']}. {lesson['title']}</b>
Пиши для новичка, без сложного жаргона (термины сразу поясняй). Не обещай гарантированную прибыль, не давай персональных финансовых советов, честно упоминай риски там, где они есть.

В конце добавь: 💬 Обсуждаем это здесь: {COMMUNITY_CHAT_LINK}

Только готовый пост, без пояснений."""

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1400,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            data = resp.json()
            if "content" in data:
                return data["content"][0]["text"]
            logger.error(f"Claude lesson error: {data}")
            _record_claude_error(data)
            return ""
    except Exception as e:
        logger.error(f"Claude lesson error: {e}")
        _record_claude_error(e)
        return ""
```

- [ ] **Step 3: `image_brief.py` — функция `generate_lesson_image_brief`** (в конец файла)

```python
# ── ТЗ для картинки-схемы к уроку ─────────────────────────────────────────────
async def generate_lesson_image_brief(lesson: dict, category: str) -> str:
    style = CATEGORY_STYLE.get(category, CATEGORY_STYLE["crypto"])
    fallback = (
        f"Чистая образовательная инфографика на тему «{lesson['title']}»: понятная схема с "
        f"2-3 короткими подписями на английском, {style}, минимализм, кинематографично."
    )
    prompt = f"""Составь короткое ТЗ на образовательную картинку-схему к уроку для новичков.

Урок: {lesson['title']}
Суть: {lesson['points']}

Это не абстрактный баннер, а наглядная схема/инфографика, которая объясняет идею урока (например: график со свечой и подписями, стрелки, блок-схема шагов).
Стиль: {style}, минимализм, хорошая читаемость.
Текст на картинке — не более 3-4 коротких подписей на английском, без длинных фраз.

Напиши ТЗ в 2-3 предложения простым текстом, без markdown."""

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
                    "max_tokens": 250,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            data = resp.json()
            if "content" in data:
                return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Lesson brief error: {e}")
    return fallback
```

- [ ] **Step 4: Проверить синтаксис и импорты**

Run: `cd /c/Users/Андрей/catapult-bot-git && python -m py_compile subagents/rewriter.py subagents/image_brief.py && python -c "from subagents.rewriter import generate_lesson_post; from subagents.image_brief import generate_lesson_image_brief; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add subagents/rewriter.py subagents/image_brief.py
git commit -m "feat: генераторы обучающего поста и ТЗ картинки-схемы"
```

---

### Task 4: Слоты постов и отключение Catapult в постах

**Files:**
- Modify: `orchestrator.py` (импорты; константа `CATAPULT_TEXT_DAYS`; `check_breaking_news`; `evening_generation`; `get_engagement_digest`; `propose_self_record_script`)
- Modify: `subagents/weekly_plan.py` (текст расписания)

**Interfaces:**
- Consumes: `edu_progress.get_lesson`, `generate_lesson_post`, `generate_lesson_image_brief` (Tasks 2-3); `auto_approve_post`, `generate_image`
- Produces: `evening_generation` заполняет слоты crypto_1/catapult_1/ai/forex/crypto_2 уроками

- [ ] **Step 1: Импорты** (строки 15 и 17)

```python
from subagents.rewriter import generate_post_claude, generate_catapult_post, generate_poll, CATAPULT_ANGLES, generate_forexbot_post, FOREXBOT_ANGLES, get_last_claude_error, generate_lesson_post
```
```python
from subagents.image_brief import generate_image_brief, generate_lesson_image_brief
from subagents import edu_progress
```
(`edu_progress` импортировать после блока `from subagents.image_generator import generate_image`.)

- [ ] **Step 2: Константа дней** — заменить блок с `CATAPULT_TEXT_DAYS`:

```python
# Дни недели (по дню публикации), когда слот 11:00 (catapult_1) отдан forex-уроку —
# бывшие Catapult-дни. Остальные дни слот 11:00 — про forexbot.
FOREX_BONUS_LESSON_DAYS = {"tue", "fri"}
```
(удалить старый комментарий и `CATAPULT_TEXT_DAYS = {"tue", "fri"}`).

- [ ] **Step 3: `check_breaking_news`** — убрать Catapult из списка:

Было: `for category in ["crypto", "ai", "forex", "catapult"]:`
Стало: `for category in ["crypto", "ai", "forex"]:`

- [ ] **Step 4: `evening_generation` — `_auto_post` принимает готовое ТЗ картинки**

Заменить внутреннюю функцию `_auto_post`:

```python
    async def _auto_post(text: str, category: str, slot: str, source: str = "", label: str = "", brief: str | None = None):
        if not text:
            reason = get_last_claude_error() or "пустой ответ от Claude"
            logger.warning(f"[{slot}] Пустой текст поста — пропускаем (сбой генерации: {reason})")
            failures.append((label or slot, reason))
            return
        if brief is None:
            brief = await generate_image_brief(text, category)
        photo_path = await generate_image(brief, f"{slot}_{int(datetime.utcnow().timestamp())}")
        await auto_approve_post(text, category, slot, brief, photo_path, source)
        await asyncio.sleep(2)

    tomorrow = datetime.now(KYIV_TZ) + timedelta(days=1)
    tomorrow_str = tomorrow.date().isoformat()
    tomorrow_day_key = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][tomorrow.weekday()]

    async def _lesson_post(category: str, slot: str, role: str, label: str, bonus: bool = False):
        lesson = await edu_progress.get_lesson(category, tomorrow_str, bonus=bonus)
        if not lesson:
            failures.append((label, "не удалось получить следующий урок"))
            return
        text = await generate_lesson_post(lesson, category, role)
        brief = await generate_lesson_image_brief(lesson, category) if text else None
        await _auto_post(text, category, slot, f"урок {lesson['number']}: {lesson['title']}", label, brief)
```

- [ ] **Step 5: `evening_generation` — заменить блоки 1-3** (от `# 1. Крипта #1 (09:00)` до конца блока `# 3. ИИ (13:00)`) на:

```python
    # 1. Крипта #1 (09:00) — основной урок дня
    await _lesson_post("crypto", "crypto_1", "lesson", "Крипта #1 (09:00)")

    # 2. Слот 11:00 (catapult_1): по вт/пт — дополнительный forex-урок, в остальные дни — forexbot
    if tomorrow_day_key in FOREX_BONUS_LESSON_DAYS:
        await _lesson_post("forex", "catapult_1", "lesson", "Forex урок (11:00)", bonus=True)
    else:
        forexbot_posts = await collect_top_posts("forexbot")
        if forexbot_posts:
            text = await generate_post_claude(forexbot_posts, "forexbot")
            await _auto_post(text, "forexbot", "catapult_1", forexbot_posts[0]["channel"], label="Forexbot (11:00)")
        else:
            angle1 = FOREXBOT_ANGLES[forexbot_angle_idx % len(FOREXBOT_ANGLES)]
            forexbot_angle_idx += 1
            text = await generate_forexbot_post(angle1)
            await _auto_post(text, "forexbot", "catapult_1", label="Forexbot (11:00)")

    # 3. ИИ (13:00)
    await _lesson_post("ai", "ai", "lesson", "AI (13:00)")
```
Также в первой строке функции `global catapult_angle_idx, forexbot_angle_idx, poll_idx` оставить как есть (`catapult_angle_idx` не используется, но не удаляем — Catapult может вернуться).

- [ ] **Step 6: `evening_generation` — заменить блоки 5-6** (от `# 5. Форекс (18:00)` до конца блока `# 6. Крипта #2 (20:00)`) на:

```python
    # 5. Форекс (18:00)
    await _lesson_post("forex", "forex", "lesson", "Forex (18:00)")

    # 6. Крипта #2 (20:00) — закрепление урока дня
    await _lesson_post("crypto", "crypto_2", "practice", "Крипта #2 (20:00)")
```

- [ ] **Step 7: Убрать Catapult из дайджеста и самозаписи**

`get_engagement_digest`: `for category in ["crypto", "ai", "forex", "catapult", "forexbot"]:` → `for category in ["crypto", "ai", "forex", "forexbot"]:`

`propose_self_record_script`: `categories = ["crypto", "ai", "forex", "catapult", "forexbot"]` → `categories = ["crypto", "ai", "forex", "forexbot"]`

- [ ] **Step 8: `weekly_plan.py` — расписание в промпте**

Заменить блок расписания:
```
Расписание каждого дня:
09:00 — Крипта (обучающий урок)
11:00 — Forexbot (по вт и пт — урок по Forex)
13:00 — ИИ (обучающий урок)
16:30 — Опрос
18:00 — Форекс (обучающий урок)
20:00 — Крипта (закрепление урока)
```
и первую фразу перед «Напиши план» дополнить: «Контент строго обучающий: уроки идут от азов к сложному. Для каждого поста укажи конкретную тему/идею.» (вместо «Для каждого поста укажи конкретную тему/идею.»).

- [ ] **Step 9: Проверка**

Run: `cd /c/Users/Андрей/catapult-bot-git && python -m py_compile orchestrator.py subagents/weekly_plan.py && grep -n "CATAPULT_TEXT_DAYS\|\"catapult\"\]" orchestrator.py | head`
Expected: компиляция без ошибок; `CATAPULT_TEXT_DAYS` не найден; в списках категорий `check_breaking_news`/дайджеста/самозаписи «catapult» нет (остаться может только в `_collect_topic_source`, `is_catapult_urgent`, `generate_catapult_post` — оставлены намеренно).

- [ ] **Step 10: Commit**

```bash
git add orchestrator.py subagents/weekly_plan.py
git commit -m "feat: посты по расписанию — обучающие уроки, Catapult убран из постов"
```

---

### Task 5: Видео — уроки, новость только если «кричащая»

**Files:**
- Modify: `subagents/yt_script.py` (добавить `generate_lesson_video_script`; forex-ветка новостного промпта)
- Modify: `orchestrator.py` (`_has_breaking_candidate`, `_generate_and_queue_video`, `generate_tomorrows_videos`)

**Interfaces:**
- Consumes: `edu_progress.get_lesson`, `is_truly_breaking`, `BREAKING_MAX_AGE_HOURS`, `collect_top_posts`
- Produces: `async generate_lesson_video_script(lesson: dict, category: str) -> dict | None` — тот же формат, что `generate_video_script`: `{"narration": str, "image_briefs": list[str]}`

- [ ] **Step 1: `yt_script.py` — заменить forex-образовательную ветку новостного сценария**

Удалить константы `FOREX_EDU_TOPICS` и `FOREX_TOPIC_INSTRUCTION`. В `generate_video_script` заменить блок выбора инструкций на:

```python
    topic_instruction = DEFAULT_TOPIC_INSTRUCTION.format(context=context)
    closing_instruction = FOREX_CLOSING_INSTRUCTION if category == "forex" else ""
```
(Новостной сценарий теперь используется только когда новость «кричащая»; призыв к автотрейдингу для forex сохранён.)

- [ ] **Step 2: `yt_script.py` — новая функция** (после `generate_video_script`, перед `_parse_script`)

```python
LESSON_VIDEO_CLOSING = {
    "forex": FOREX_CLOSING_INSTRUCTION,
    "crypto": "\nВ конце — короткий призыв подписаться, чтобы не пропустить следующие уроки по трейдингу на крипте (1 предложение).",
    "ai": "\nВ конце — короткий призыв подписаться, чтобы не пропустить следующие уроки по ИИ (1 предложение).",
}

async def generate_lesson_video_script(lesson: dict, category: str) -> dict | None:
    style = CATEGORY_STYLE.get(category, CATEGORY_STYLE["crypto"])
    prompt = f"""Ты — автор вертикальных YouTube Shorts для канала «Крипта, AI, Forex. Как заработать?».

Сценарий пишется для озвучки диктором (TTS) — только то, что должно прозвучать. Без эмодзи, без HTML-тегов, без ремарок в скобках.
Стиль: живо, по делу, крючок в первые 2 секунды, 90-150 слов (30-60 секунд речи).

Это ОБУЧАЮЩИЙ ролик — короткая версия урока №{lesson['number']}: {lesson['title']}
Суть: {lesson['points']}
Объясняй просто, для новичка, с одним конкретным примером. Не обещай гарантированной прибыли, не давай персональных финансовых советов.
{LESSON_VIDEO_CLOSING.get(category, '')}

Напиши сценарий и 2-4 ТЗ для картинок-схем, которые сменяют друг друга под озвучку. Каждое ТЗ — наглядная схема/инфографика по теме урока (не абстрактный баннер), стиль: {style}, не более 3-4 коротких подписей на английском.

{NEUTRALITY_NOTE}

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

- [ ] **Step 2b: Импорт и функция проверки в `orchestrator.py`**

В импорт `yt_script` добавить `generate_lesson_video_script`:
```python
from subagents.yt_script import generate_video_script, generate_self_record_script, generate_video_metadata, generate_lesson_video_script
```
Перед `# ── Сбор горячих тем по категории` добавить:

```python
EDU_VIDEO_CATEGORIES = {"crypto", "forex", "ai"}

async def _has_breaking_candidate(category: str) -> bool:
    """True, если лучший свежий пост по рубрике проходит проверку «кричащей» новости
    (те же критерии, что у часового механизма горячих постов)."""
    posts = await collect_top_posts(category)
    if not posts:
        return False
    top = posts[0]
    if top.get("date"):
        age_hours = (datetime.utcnow() - top["date"]).total_seconds() / 3600
        if age_hours > BREAKING_MAX_AGE_HOURS:
            return False
    return await is_truly_breaking(top["text"])
```

- [ ] **Step 3: `_generate_and_queue_video` — выбор сценария**

Заменить сигнатуру и начало функции (строки от `async def _generate_and_queue_video` до `script_data = await generate_video_script(topic_source, category)` включительно):

```python
async def _generate_and_queue_video(category: str, planned_day: str, planned_time: str, lesson_date: str | None = None):
    script_data = None
    if category in EDU_VIDEO_CATEGORIES and not await _has_breaking_candidate(category):
        day = lesson_date or (datetime.now(KYIV_TZ) + timedelta(days=1)).date().isoformat()
        lesson = await edu_progress.get_lesson(category, day)
        if lesson:
            script_data = await generate_lesson_video_script(lesson, category)
        else:
            logger.warning(f"_generate_and_queue_video[{category}]: не удалось получить урок")
    else:
        topic_source = await _collect_topic_source(category)
        script_data = await generate_video_script(topic_source, category)
```
Остальная функция (`if not script_data: ...`) без изменений.

- [ ] **Step 4: `generate_tomorrows_videos` — передать дату урока**

```python
    tomorrow_str = tomorrow.date().isoformat()
    for entry in entries:
        planned_time = f'{entry["hour"]:02d}:{entry["minute"]:02d}'
        await _generate_and_queue_video(entry["category"], entry["day"], planned_time, lesson_date=tomorrow_str)
        await asyncio.sleep(2)
```
(строка `tomorrow_str` — сразу после вычисления `tomorrow`, до `for`).

- [ ] **Step 5: Проверка**

Run: `cd /c/Users/Андрей/catapult-bot-git && python -m py_compile subagents/yt_script.py orchestrator.py && python -c "import orchestrator" 2>&1 | tail -3`
Expected: py_compile без ошибок. Импорт `orchestrator` локально может упасть на `os.makedirs('/data/...')`/токенах окружения — это допустимо; главное, чтобы не было `ImportError`/`NameError`/`SyntaxError`.

- [ ] **Step 6: Commit**

```bash
git add subagents/yt_script.py orchestrator.py
git commit -m "feat: видео crypto/forex/ai по урокам, новость — только кричащая"
```

---

### Task 6: Пробный прогон с превью админу (без публикации)

**Files:**
- Create (вне репозитория, в scratchpad): `preview_edu.py`

**Interfaces:**
- Consumes: все функции Tasks 2-5. Запускается локально через `railway run` (боевые ключи из окружения), прогресс — во временный файл (реальный не двигается), картинки — во временную папку, отправка только админу через бота модерации.

- [ ] **Step 1: Скрипт превью**

```python
import asyncio, os, sys, tempfile, httpx
sys.path.insert(0, r"C:\Users\Андрей\catapult-bot-git")
sys.stdout.reconfigure(encoding="utf-8")
from subagents import edu_progress, image_generator
from subagents.rewriter import generate_lesson_post
from subagents.image_brief import generate_lesson_image_brief
from subagents.yt_script import generate_lesson_video_script

image_generator.PHOTOS_DIR = tempfile.mkdtemp()
PROGRESS = os.path.join(tempfile.mkdtemp(), "p.json")
BOT = os.getenv("PARSER_BOT_TOKEN"); ADMIN = int(os.getenv("ADMIN_TG_ID", "0"))

async def send(client, method, **kw):
    r = await client.post(f"https://api.telegram.org/bot{BOT}/{method}", **kw)
    print(method, r.status_code)

async def main():
    async with httpx.AsyncClient(timeout=60) as c:
        await send(c, "sendMessage", json={"chat_id": ADMIN, "text": "🧪 ПРЕВЬЮ обучающего контента (не опубликовано)"})
        for cat in ["crypto", "forex", "ai"]:
            lesson = await edu_progress.get_lesson(cat, "2099-01-01", path=PROGRESS)
            text = await generate_lesson_post(lesson, cat, "lesson")
            brief = await generate_lesson_image_brief(lesson, cat)
            photo = await image_generator.generate_image(brief, f"preview_{cat}")
            print(cat, lesson["number"], lesson["title"], "| photo:", bool(photo))
            if photo:
                with open(photo, "rb") as f:
                    await send(c, "sendPhoto", data={"chat_id": ADMIN, "caption": f"[{cat}] ТЗ: {brief[:900]}"}, files={"photo": f})
            await send(c, "sendMessage", json={"chat_id": ADMIN, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        lesson = await edu_progress.get_lesson("forex", "2099-01-02", path=PROGRESS)
        script = await generate_lesson_video_script(lesson, "forex")
        await send(c, "sendMessage", json={"chat_id": ADMIN, "text": f"🎬 Сценарий forex-видео (урок {lesson['number']}):\n\n{script['narration'] if script else 'СБОЙ'}"})

asyncio.run(main())
```

- [ ] **Step 2: Запуск**

Run: `cd /c/Users/Андрей/catapult-bot-git && PYTHONIOENCODING=utf-8 railway run python <путь к preview_edu.py>` (CLI должен быть привязан к Catapult-Bot).
Expected: строки `sendPhoto 200`/`sendMessage 200` ×3 рубрики + сценарий видео; у каждой рубрики `photo: True`.

- [ ] **Step 3: Показать превью пользователю** — админу пришли 3 поста, 3 картинки и сценарий forex-видео. Дождаться его «ок»; при замечаниях (стиль, длина, надписи на картинках) — поправить промпты в Tasks 3/5 и повторить Step 2.

---

### Task 7: Выкладка в прод

- [ ] **Step 1: Финальный просмотр диффа** (`git diff origin/main --stat` и чтение всего диффа) и `py_compile` всех изменённых файлов.
- [ ] **Step 2: Пуш и деплой**

```bash
cd /c/Users/Андрей/catapult-bot-git && git push
```
Проверить `mcp__railway__list_deployments` (SUCCESS, не CRASHED), логи на traceback.
- [ ] **Step 3: Остановить `exciting-patience`** (`railway link ... -s e1d6be12-b4b2-45f4-a9cc-f650a8971cc3` → `railway down -y` → проверить `environment_status`), затем вернуть привязку CLI на Catapult-Bot (`-s 82216bd1-88e4-4581-b1d1-dcc60dc6340d`).
- [ ] **Step 4: Наблюдение** — в 20:00 по Киеву проверить, что вечерняя генерация прошла на новых уроках (сообщения админу «Авто-одобрено», отсутствие «С ОШИБКАМИ»), файл `/data/edu_progress.json` создался, в очереди на завтра слоты crypto_1/ai/forex/crypto_2 (+catapult_1).
- [ ] **Step 5: Память** — обновить `project_catapult_bot.md` (сессия 19.09: аудит платных сервисов, наверстывание, переход на обучающий контент, исправить «13 видео/неделю» → 14) и индекс `MEMORY.md`.

---

## Self-Review

- **Покрытие спеки:** §1 банк → Task 1; §2 прогресс (+достройка Claude) → Task 2; §3 тексты → Task 3; §4 картинки → Task 3; §5 слоты постов → Task 4; §6 видео (новость только если кричащая, forexbot без изменений) → Task 5; §7 отключение Catapult (breaking, digest, self-record, weekly_plan; evening-ветка) → Task 4; §8 не меняется — файлы расписания видео/публикации не затронуты; проверка/превью → Task 6; выкладка → Task 7.
- **Плейсхолдеры:** нет; весь код и замены приведены.
- **Согласованность типов:** `get_lesson(category, day, bonus=False, path=...)` и `peek_or_assign(...)` одинаковы в Tasks 2/4/5; `generate_lesson_post(lesson, category, role)`, `generate_lesson_image_brief(lesson, category)`, `generate_lesson_video_script(lesson, category)` используются с теми же сигнатурами; `_generate_and_queue_video(..., lesson_date=None)` вызывается с `lesson_date=tomorrow_str` из `generate_tomorrows_videos`.
