"""
Дирижёр (orchestrator): связывает мониторинг -> rewrite -> image-brief ->
approval для контент-пайплайна Telegram. Перенесено из parser.py (бывшая
evening_generation) без изменения логики.
"""
import os
import json
import asyncio
import logging
from datetime import datetime, date

import httpx

from subagents.tg_monitor import collect_top_posts, sent_hashes, viral_score
from subagents.rewriter import generate_post_claude, generate_catapult_post, generate_poll, CATAPULT_ANGLES
from subagents.tg_publisher import pending_posts, approved_queue, send_for_approval, approval_keyboard, auto_approve_post, publish_now_auto, queue_action_keyboard, breaking_already_posted_today, catapult_already_posted_today, mark_breaking_posted
from subagents.image_brief import generate_image_brief
from subagents.image_generator import generate_image
from subagents.yt_ideas import get_trending_shorts_ideas
from subagents.yt_script import generate_video_script, generate_self_record_script, generate_video_metadata
from subagents.yt_voice import generate_voiceover
from subagents.yt_render import render_video
from subagents.yt_publisher import send_video_for_approval, awaiting_self_record_video, create_upload_token, pop_pending_uploads

logger = logging.getLogger(__name__)

# ── Конфиг ────────────────────────────────────────────────────────────────────
# Read directly from env (not via subagents.tg_publisher.configure()) because
# configure() runs later in parser.py's main() — importing these names from
# tg_publisher here would capture None at import time. Same env vars either way.
PARSER_BOT_TOKEN = os.getenv("PARSER_BOT_TOKEN")
ADMIN_TG_ID      = int(os.getenv("ADMIN_TG_ID", "0"))
BACKEND_URL = os.getenv("BACKEND_URL", "https://web-production-9851f.up.railway.app")

# ── Расписание публикаций (следующий день) ────────────────────────────────────
PUBLISH_SCHEDULE = [
    {"hour": 9,  "minute": 0,  "slot": "crypto_1"},
    {"hour": 11, "minute": 0,  "slot": "catapult_1"},
    {"hour": 13, "minute": 0,  "slot": "ai"},
    {"hour": 16, "minute": 30, "slot": "poll"},
    {"hour": 18, "minute": 0,  "slot": "forex"},
    {"hour": 20, "minute": 0,  "slot": "crypto_2"},
]

# ── Темы опросов (fallback, если генерация через Claude не удалась) ───────────
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
catapult_angle_idx: int = 0   # текущий угол Catapult
poll_idx: int = 0             # текущий fallback-опрос (если генерация не удалась)
last_poll_date: str = ""      # дата последнего опубликованного опроса (YYYY-MM-DD) — для логики "раз в 2 дня"
short_category_idx: int = 0        # текущая категория для авто-Short
self_record_category_idx: int = 0  # текущая категория для предложения самозаписи
recent_poll_questions: list = []  # последние заданные вопросы — чтобы Claude не повторялся

CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
BREAKING_MAX_AGE_HOURS = 4  # пост старше 4 часов — уже не горячий

# ── Персистентность состояния опроса (переживает рестарты контейнера) ────────
POLL_STATE_FILE = "/data/poll_state.json"

def load_poll_state():
    global last_poll_date
    try:
        if os.path.exists(POLL_STATE_FILE):
            with open(POLL_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                last_poll_date = state.get("last_poll_date", "")
                recent_poll_questions[:] = state.get("recent_questions", [])
    except Exception as e:
        logger.error(f"Load poll state error: {e}")

def save_poll_state():
    try:
        with open(POLL_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_poll_date": last_poll_date, "recent_questions": recent_poll_questions}, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Save poll state error: {e}")

# ── Семантическая проверка: устареет ли новость к завтрашнему утру ────────────
async def is_truly_breaking(post_text: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 12,
                    "messages": [{
                        "role": "user",
                        "content": (
                            "Ты редактор финансового Telegram-канала о крипте, форексе и ИИ.\n"
                            "Тебе показывают пост из стороннего канала.\n"
                            "Публикация СЕЙЧАС — это редкое исключение, не правило. По умолчанию правильный ответ — ЖДАТЬ.\n"
                            "В 9 из 10 случаев правильный ответ ЖДАТЬ, даже если новость свежая и заметная.\n\n"
                            "ПУБЛИКОВАТЬ СЕЙЧАС — только если это КРУПНОЕ, однозначно экстренное событие:\n"
                            "— Обвал/взлёт рынка на 10%+ в моменте\n"
                            "— Крупный хак/эксплойт с потерями от $10M+\n"
                            "— Банкротство, делистинг или взлом крупной биржи/протокола\n"
                            "— Официальное решение регулятора (запрет, одобрение ETF, судебное решение)\n"
                            "— Арест или уголовное дело против крупной публичной фигуры\n"
                            "— Заявление первых лиц государств, напрямую двигающее рынок\n\n"
                            "ЖДАТЬ ДО ВЕЧЕРА — если это:\n"
                            "— Обычное движение цены, даже заметное, но не экстремальное\n"
                            "— Аналитика, прогнозы, мнения инфлюенсеров, обучающий контент\n"
                            "— Партнёрства, апдейты продукта, листинг токена, некритичные новости проекта\n"
                            "— Личный кейс/история трейдера\n"
                            "— Слухи и неподтверждённые вбросы\n\n"
                            f"Пост:\n{post_text[:700]}\n\n"
                            "Один ответ: СЕЙЧАС или ЖДАТЬ"
                        )
                    }]
                }
            )
            answer = resp.json()["content"][0]["text"].strip().upper()
            result = "СЕЙЧАС" in answer
            logger.info(f"is_truly_breaking → {answer} → {'ДА' if result else 'НЕТ'}")
            return result
    except Exception as e:
        logger.warning(f"is_truly_breaking error: {e}")
        return False

# ── Семантическая проверка: есть ли у поста Catapult ограниченное окно/дедлайн ─
async def is_catapult_urgent(post_text: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 12,
                    "messages": [{
                        "role": "user",
                        "content": (
                            "Ты редактор Telegram-канала, продвигающего платформу Catapult Trade через реферальную программу.\n"
                            "Тебе показывают пост из официального канала Catapult Trade.\n"
                            "Вопрос: если опубликовать этот пост только завтра вечером — читатель упустит реальную возможность или дедлайн?\n\n"
                            "ПУБЛИКОВАТЬ СЕЙЧАС — если пост объявляет:\n"
                            "— Ограниченное по времени окно доступа (whitelist, early access, pre-sale)\n"
                            "— Конкретный дедлайн или обратный отсчёт (часы/дни до события)\n"
                            "— Старт или окончание продажи токена, листинга, акции\n"
                            "— Любое \"успей/только сейчас/осталось N часов\"\n\n"
                            "ЖДАТЬ ДО ВЕЧЕРА — если это:\n"
                            "— Общие новости о партнёрствах, обновлениях продукта без дедлайна\n"
                            "— Аналитика, статистика, отчёты\n"
                            "— Обычные образовательные/маркетинговые посты без временного окна\n\n"
                            f"Пост:\n{post_text[:700]}\n\n"
                            "Один ответ: СЕЙЧАС или ЖДАТЬ"
                        )
                    }]
                }
            )
            answer = resp.json()["content"][0]["text"].strip().upper()
            result = "СЕЙЧАС" in answer
            logger.info(f"is_catapult_urgent → {answer} → {'ДА' if result else 'НЕТ'}")
            return result
    except Exception as e:
        logger.warning(f"is_catapult_urgent error: {e}")
        return False

# ── Проверка горячих новостей (каждый час) ────────────────────────────────────
async def check_breaking_news():
    logger.info("=== Проверка горячих новостей ===")
    for category in ["crypto", "ai", "forex", "catapult"]:
        if breaking_already_posted_today(category):
            logger.info(f"[{category}] Горячая новость уже публиковалась сегодня — пропускаем до завтра")
            continue
        if category == "catapult" and catapult_already_posted_today():
            logger.info("[catapult] Catapult-пост уже был сегодня (плановый или горячий) — пропускаем")
            continue

        posts = await collect_top_posts(category)
        if not posts:
            continue
        top = posts[0]

        # Пропускаем уже обработанные
        if top["hash"] in sent_hashes:
            continue

        # Пропускаем посты старше 4 часов — они уже не горячие
        if top.get("date"):
            age_hours = (datetime.utcnow() - top["date"]).total_seconds() / 3600
            if age_hours > BREAKING_MAX_AGE_HOURS:
                logger.info(f"[{category}] Посту {age_hours:.1f}ч — слишком старый, пропускаем")
                continue

        # Claude решает: срочно ли публиковать (свой критерий для catapult)
        is_urgent = await is_catapult_urgent(top["text"]) if category == "catapult" else await is_truly_breaking(top["text"])
        if not is_urgent:
            logger.info(f"[{category}] Claude: не срочно — пропускаем")
            sent_hashes.add(top["hash"])
            continue

        logger.info(f"[{category}] 🔥 Горячая новость подтверждена!")
        sent_hashes.add(top["hash"])
        try:
            text = await generate_post_claude(posts, category)
            if not text:
                logger.warning(f"[{category}] Пустой текст поста — пропускаем (сбой генерации)")
                continue
            slot = f"breaking_{category}_{int(datetime.utcnow().timestamp())}"
            brief = await generate_image_brief(text, category)
            photo_path = await generate_image(brief, slot)
            await publish_now_auto(text, category, slot, brief, photo_path, top["channel"])
            mark_breaking_posted(category)
        except Exception as e:
            logger.error(f"check_breaking_news [{category}] error: {e}")
        await asyncio.sleep(2)

# ── Вечерняя генерация (20:00) ────────────────────────────────────────────────
async def evening_generation():
    global catapult_angle_idx, poll_idx
    logger.info("=== Вечерняя генерация постов ===")

    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": "🌙 <b>Начинаю подготовку постов на завтра...</b>\n\nСобираю новости и генерирую контент.",
                "parse_mode": "HTML"
            }
        )

    async def _auto_post(text: str, category: str, slot: str, source: str = ""):
        if not text:
            logger.warning(f"[{slot}] Пустой текст поста — пропускаем (сбой генерации)")
            return
        brief = await generate_image_brief(text, category)
        photo_path = await generate_image(brief, f"{slot}_{int(datetime.utcnow().timestamp())}")
        await auto_approve_post(text, category, slot, brief, photo_path, source)
        await asyncio.sleep(2)

    # 1. Крипта #1 (09:00)
    crypto_posts = await collect_top_posts("crypto")
    if crypto_posts:
        text = await generate_post_claude(crypto_posts, "crypto")
        await _auto_post(text, "crypto", "crypto_1", crypto_posts[0]["channel"])
        for p in crypto_posts:
            sent_hashes.add(p["hash"])

    # 2. Catapult #1 (11:00)
    catapult_posts = await collect_top_posts("catapult")
    if catapult_posts:
        text = await generate_post_claude(catapult_posts, "catapult")
        await _auto_post(text, "catapult", "catapult_1", catapult_posts[0]["channel"])
    else:
        angle1 = CATAPULT_ANGLES[catapult_angle_idx % len(CATAPULT_ANGLES)]
        catapult_angle_idx += 1
        text = await generate_catapult_post(angle1)
        await _auto_post(text, "catapult", "catapult_1")

    # 3. ИИ (13:00)
    ai_posts = await collect_top_posts("ai")
    if ai_posts:
        text = await generate_post_claude(ai_posts, "ai")
        await _auto_post(text, "ai", "ai", ai_posts[0]["channel"])

    # 4. Опрос (раз в 2 дня — 16:30) — авто-одобряем, с картинкой
    global last_poll_date
    today = datetime.utcnow().date()
    last_date = None
    if last_poll_date:
        try:
            last_date = date.fromisoformat(last_poll_date)
        except ValueError:
            last_date = None

    if last_date is None or (today - last_date).days >= 2:
        last_poll_date = today.isoformat()
        poll = await generate_poll(recent_poll_questions)
        if not poll:
            poll = POLL_TOPICS[poll_idx % len(POLL_TOPICS)]
            poll_idx += 1
        recent_poll_questions.append(poll["question"])
        del recent_poll_questions[:-20]
        save_poll_state()

        poll_text = f"📊 <b>ОПРОС</b>\n\n{poll['question']}\n\n" + "\n".join([f"• {o}" for o in poll['options']])
        brief = await generate_image_brief(poll["question"], "poll")
        photo_path = await generate_image(brief, f"poll_{int(datetime.utcnow().timestamp())}")

        approved_queue["poll"] = {
            "text": poll_text,
            "original": poll_text,
            "category": "poll",
            "slot": "poll",
            "source": "",
            "brief": brief,
            "photo_path": photo_path,
            "poll_data": poll,
        }
        from subagents.tg_publisher import save_approved
        save_approved()
        photo_status = "🖼 С картинкой" if photo_path else "📝 Без картинки"
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": ADMIN_TG_ID,
                    "text": (
                        f"🤖 <b>Авто-одобрено:</b> 📊 ОПРОС\n"
                        f"📅 Публикация завтра в 16:30 | {photo_status}\n\n"
                        f"{poll['question']}\nВарианты: {' / '.join(poll['options'])}"
                    ),
                    "parse_mode": "HTML",
                    "reply_markup": queue_action_keyboard("poll"),
                }
            )
        await asyncio.sleep(2)
    else:
        days_left = 2 - (today - last_date).days
        logger.info(f"Опрос пропущен — последний был {last_poll_date}, следующий через {days_left} дн.")

    # 5. Форекс (18:00)
    forex_posts = await collect_top_posts("forex")
    if forex_posts:
        text = await generate_post_claude(forex_posts, "forex")
        await _auto_post(text, "forex", "forex", forex_posts[0]["channel"])

    # 6. Крипта #2 (20:00)
    crypto_posts_evening = await collect_top_posts("crypto")
    if crypto_posts_evening:
        text = await generate_post_claude(crypto_posts_evening, "crypto")
        await _auto_post(text, "crypto", "crypto_2", crypto_posts_evening[0]["channel"])
    elif crypto_posts:
        text = await generate_post_claude(crypto_posts, "crypto")
        await _auto_post(text, "crypto", "crypto_2", crypto_posts[0]["channel"])

    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": "✅ <b>Все посты подготовлены и одобрены автоматически!</b>\n\nОни опубликуются завтра по расписанию без твоего участия.",
                "parse_mode": "HTML"
            }
        )

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

    upload_token = create_upload_token(script_data["topic"], script_data["script"], category)
    upload_url = f"{BACKEND_URL}/upload/{upload_token}"

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
                    f"Запиши видео на эту тему и пришли файл сюда — я подготовлю название, описание и отправлю на одобрение.\n\n"
                    f"📎 Если ролик больше ~15 МБ, Telegram не даст мне его скачать напрямую — вместо этого загрузи его тут: {upload_url}"
                ),
                "parse_mode": "HTML",
            },
        )

# ── Обработка видео, загруженных через /upload (для роликов больше лимита Telegram) ──
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
