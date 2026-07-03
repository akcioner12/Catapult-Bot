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
from subagents.tg_publisher import pending_posts, approved_queue, send_for_approval, approval_keyboard, auto_approve_post, publish_now_auto
from subagents.image_brief import generate_image_brief
from subagents.image_generator import generate_image

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

BREAKING_SCORE_THRESHOLD = int(os.getenv("BREAKING_SCORE_THRESHOLD", "20000"))
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")

# ── Семантическая проверка: реально ли новость срочная ────────────────────────
async def is_truly_breaking(post_text: str) -> bool:
    """Спрашивает Claude — влияет ли эта новость на рынок ПРЯМО СЕЙЧАС."""
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
                    "max_tokens": 5,
                    "messages": [{
                        "role": "user",
                        "content": (
                            "Ты анализируешь новость из крипто/форекс/ИИ канала.\n"
                            "Вопрос: эта новость требует публикации ПРЯМО СЕЙЧАС (в ближайшие 1-2 часа), "
                            "потому что она влияет на поведение рынка прямо сейчас?\n\n"
                            "СРОЧНО (публиковать сейчас):\n"
                            "— Биржа взломана или остановила вывод средств\n"
                            "— Обвал или рост цены >10% за последний час\n"
                            "— Регуляторный запрет или арест объявлен сегодня\n"
                            "— Крупное банкротство или скам раскрыт сегодня\n"
                            "— Экстренное решение ФРС или центробанка\n\n"
                            "НЕ СРОЧНО (подождёт до вечера):\n"
                            "— Кто-то заработал на трейдинге\n"
                            "— Прогнозы и аналитика\n"
                            "— Образовательный контент\n"
                            "— Обычные движения рынка\n"
                            "— Новости о продуктах и партнёрствах\n\n"
                            f"Новость:\n{post_text[:600]}\n\n"
                            "Ответь одним словом: СРОЧНО или ЖДАТЬ"
                        )
                    }]
                }
            )
            answer = resp.json()["content"][0]["text"].strip().upper()
            result = "СРОЧНО" in answer
            logger.info(f"is_truly_breaking → {answer} → {'ДА' if result else 'НЕТ'}")
            return result
    except Exception as e:
        logger.warning(f"is_truly_breaking error: {e}")
        return False

# ── Проверка горячих новостей (каждые 3 часа) ─────────────────────────────────
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
        # Второй фильтр: Claude решает — реально ли это срочно
        if not await is_truly_breaking(top["text"]):
            logger.info(f"[{category}] Скор высокий, но Claude решил: не срочно — пропускаем")
            sent_hashes.add(top["hash"])  # помечаем чтобы не проверять снова
            continue
        logger.info(f"[{category}] 🔥 Подтверждена горячая новость! Скор {score:.0f}")
        sent_hashes.add(top["hash"])
        try:
            text = await generate_post_claude(posts, category)
            slot = f"breaking_{category}_{int(datetime.utcnow().timestamp())}"
            brief = await generate_image_brief(text, category)
            photo_path = await generate_image(brief, slot)
            await publish_now_auto(text, category, slot, brief, photo_path, top["channel"])
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

    # 4. Опрос (через день — 16:30) — авто-одобряем без картинки (это Telegram-опрос)
    global last_poll_date
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    if last_poll_date != today_str:
        last_poll_date = today_str
        poll = POLL_TOPICS[poll_idx % len(POLL_TOPICS)]
        poll_idx += 1
        poll_text = f"📊 <b>ОПРОС</b>\n\n{poll['question']}\n\n" + "\n".join([f"• {o}" for o in poll['options']])
        poll_id = f"poll_{hashlib.md5(poll['question'].encode()).hexdigest()[:8]}"
        approved_queue["poll"] = {
            "text": poll_text,
            "original": poll_text,
            "category": "poll",
            "slot": "poll",
            "source": "",
            "brief": "",
            "poll_data": poll,
        }
        from subagents.tg_publisher import save_approved
        save_approved()
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": ADMIN_TG_ID,
                    "text": f"🤖 <b>Авто-одобрено:</b> 📊 ОПРОС\n📅 Публикация завтра в 16:30\n\n{poll['question']}\nВарианты: {' / '.join(poll['options'])}",
                    "parse_mode": "HTML",
                }
            )
        await asyncio.sleep(2)
    else:
        logger.info("Опрос сегодня пропущен — был вчера (логика 'через день')")

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
