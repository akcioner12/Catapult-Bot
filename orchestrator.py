"""
Дирижёр (orchestrator): связывает мониторинг -> rewrite -> image-brief ->
approval для контент-пайплайна Telegram. Перенесено из parser.py (бывшая
evening_generation) без изменения логики.
"""
import os
import asyncio
import hashlib
import logging
from datetime import datetime

import httpx

from subagents.tg_monitor import collect_top_posts, sent_hashes, viral_score
from subagents.rewriter import generate_post_claude, generate_catapult_post, CATAPULT_ANGLES
from subagents.tg_publisher import pending_posts, send_for_approval, approval_keyboard

logger = logging.getLogger(__name__)

# ── Конфиг ────────────────────────────────────────────────────────────────────
# Read directly from env (not via subagents.tg_publisher.configure()) because
# configure() runs later in parser.py's main() — importing these names from
# tg_publisher here would capture None at import time. Same env vars either way.
PARSER_BOT_TOKEN = os.getenv("PARSER_BOT_TOKEN")
ADMIN_TG_ID      = int(os.getenv("ADMIN_TG_ID", "0"))

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
catapult_angle_idx: int = 0   # текущий угол Catapult
poll_idx: int = 0             # текущий опрос
last_poll_date: str = ""      # дата последнего опубликованного опроса (YYYY-MM-DD) — для логики "через день"

# Порог вирусности для немедленной публикации (просмотры / часы).
# Например: 5000 = пост с 10к просмотров за 2 часа. Увеличь если слишком много ложных тревог.
BREAKING_SCORE_THRESHOLD = int(os.getenv("BREAKING_SCORE_THRESHOLD", "5000"))

# ── Проверка горячих новостей (каждые 2 часа) ─────────────────────────────────
async def check_breaking_news():
    logger.info("=== Проверка горячих новостей ===")
    for category in ["crypto", "ai", "forex"]:
        posts = await collect_top_posts(category)
        if not posts:
            continue
        top = posts[0]
        score = viral_score(top)
        if score < BREAKING_SCORE_THRESHOLD:
            logger.info(f"[{category}] Скор {score:.0f} — ниже порога {BREAKING_SCORE_THRESHOLD}, пропускаем")
            continue
        if top["hash"] in sent_hashes:
            logger.info(f"[{category}] Пост уже был отправлен, пропускаем")
            continue
        logger.info(f"[{category}] 🔥 Горячая новость! Скор {score:.0f} — генерирую пост")
        sent_hashes.add(top["hash"])
        try:
            text = await generate_post_claude(posts, category)
            slot = f"breaking_{category}_{int(datetime.utcnow().timestamp())}"
            await send_for_approval(text, category, slot, top["channel"], top["text"], breaking=True)
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

    # 1. Крипта #1 (09:00)
    crypto_posts = await collect_top_posts("crypto")
    if crypto_posts:
        text = await generate_post_claude(crypto_posts, "crypto")
        await send_for_approval(text, "crypto", "crypto_1", crypto_posts[0]["channel"], crypto_posts[0]["text"])
        # Помечаем использованные новости как "отправленные" — чтобы вечерний сбор их не повторил
        for p in crypto_posts:
            sent_hashes.add(p["hash"])
        await asyncio.sleep(2)

    # 2. Catapult #1 (11:00) — сначала пробуем реальные новости, иначе старый механизм "углов"
    catapult_posts = await collect_top_posts("catapult")
    if catapult_posts:
        text = await generate_post_claude(catapult_posts, "catapult")
        await send_for_approval(text, "catapult", "catapult_1", catapult_posts[0]["channel"], catapult_posts[0]["text"])
    else:
        angle1 = CATAPULT_ANGLES[catapult_angle_idx % len(CATAPULT_ANGLES)]
        catapult_angle_idx += 1
        text = await generate_catapult_post(angle1)
        await send_for_approval(text, "catapult", "catapult_1")
    await asyncio.sleep(2)

    # 3. ИИ (13:00)
    ai_posts = await collect_top_posts("ai")
    if ai_posts:
        text = await generate_post_claude(ai_posts, "ai")
        await send_for_approval(text, "ai", "ai", ai_posts[0]["channel"], ai_posts[0]["text"])
        await asyncio.sleep(2)

    # 5. Опрос (через день — 16:30)
    global last_poll_date
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    if last_poll_date != today_str:
        last_poll_date = today_str
        poll = POLL_TOPICS[poll_idx % len(POLL_TOPICS)]
        poll_idx += 1
        poll_text = f"📊 <b>ОПРОС</b>\n\n{poll['question']}\n\n" + "\n".join([f"• {o}" for o in poll['options']])
        poll_id = f"poll_{hashlib.md5(poll['question'].encode()).hexdigest()[:8]}"
        pending_posts[poll_id] = {
            "text": poll_text,
            "original": poll_text,
            "category": "poll",
            "slot": "poll",
            "source": "",
            "brief": "Яркая карточка с вопросом, тёмный фон, неоновый текст, 1200x630px.",
            "poll_data": poll,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": ADMIN_TG_ID,
                    "text": (
                        f"📌 <b>📊 ОПРОС</b> | публикация завтра в 16:30\n"
                        f"{'─' * 28}\n"
                        f"{poll['question']}\n\n"
                        f"Варианты: {' / '.join(poll['options'])}\n"
                        f"{'─' * 28}\n"
                        f"🖼 <b>ТЗ для картинки:</b>\nЯркая карточка с вопросом, тёмный фон, неоновый текст."
                    ),
                    "parse_mode": "HTML",
                    "reply_markup": approval_keyboard(poll_id)
                }
            )
        await asyncio.sleep(2)
    else:
        logger.info("Опрос сегодня пропущен — был вчера (логика 'через день')")

    # 6. Форекс (18:00)
    forex_posts = await collect_top_posts("forex")
    if forex_posts:
        text = await generate_post_claude(forex_posts, "forex")
        await send_for_approval(text, "forex", "forex", forex_posts[0]["channel"], forex_posts[0]["text"])
        await asyncio.sleep(2)

    # 7. Крипта #2 (20:00) — пересобираем новости отдельно, чтобы не дублировать утренний пост
    crypto_posts_evening = await collect_top_posts("crypto")
    if crypto_posts_evening:
        text = await generate_post_claude(crypto_posts_evening, "crypto")
        await send_for_approval(text, "crypto", "crypto_2", crypto_posts_evening[0]["channel"], crypto_posts_evening[0]["text"])
    elif crypto_posts:
        # фоллбэк на утренние посты, если вечерний сбор не дал результатов
        text = await generate_post_claude(crypto_posts, "crypto")
        await send_for_approval(text, "crypto", "crypto_2", crypto_posts[0]["channel"], crypto_posts[0]["text"])

    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": "✅ <b>Все посты готовы!</b>\n\nОдобри или отредактируй каждый — они опубликуются завтра автоматически по расписанию.",
                "parse_mode": "HTML"
            }
        )
