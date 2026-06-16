"""
Автопарсер контента v7
- Ежевечерний сбор постов (20:00) для публикации на следующий день
- 7 постов в день: Крипта, Catapult, ИИ, Catapult, Опрос, Форекс, Крипта
- Одобрение через кнопки: ✅ Одобрить / ✏️ Редактировать / 🔄 Переписать / ❌ Отменить
- После одобрения — автопубликация по расписанию
- ТЗ для картинки к каждому посту
- Воскресный контент-план в 19:00
"""

import os
import asyncio
import logging
import hashlib
import json
import random
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Конфиг ────────────────────────────────────────────────────────────────────
PARSER_BOT_TOKEN = os.getenv("PARSER_BOT_TOKEN")
ADMIN_TG_ID      = int(os.getenv("ADMIN_TG_ID", "0"))
CHANNEL_ID       = os.getenv("CHANNEL_ID", "@Crypto_AI_Forex")
CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY", "")
MAIN_BOT_TOKEN   = os.getenv("BOT_TOKEN")
TGSTAT_TOKEN     = os.getenv("TGSTAT_TOKEN", "")
CLAUDE_API_URL   = "https://api.anthropic.com/v1/messages"
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
        "neurobussines", "naebnet", "neyroseti_dr", "loading100ai"
    ],
    "forex": [
        "PROFiInvest", "tradeforexexchange", "premiumgolubev",
        "markoptions", "newwavetrade", "goldenonemoney", "uiartemzvezdin"
    ]
}

# ── Расписание публикаций (следующий день) ────────────────────────────────────
PUBLISH_SCHEDULE = [
    {"hour": 9,  "minute": 0,  "slot": "crypto_1"},
    {"hour": 11, "minute": 0,  "slot": "catapult_1"},
    {"hour": 13, "minute": 0,  "slot": "ai"},
    {"hour": 15, "minute": 0,  "slot": "catapult_2"},
    {"hour": 16, "minute": 30, "slot": "poll"},
    {"hour": 18, "minute": 0,  "slot": "forex"},
    {"hour": 20, "minute": 0,  "slot": "crypto_2"},
]

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

# ── Подпись канала ────────────────────────────────────────────────────────────
CHANNEL_SIGNATURE = """

———
🔔 Подпишись и не пропусти важное

▶️ <a href="#">YouTube</a> | 💬 <a href="https://t.me/Crypto_AI_Forex_Chat">TG Chat</a> | 🎵 <a href="#">TikTok</a> | 📷 <a href="#">Instagram</a> | 🤖 <a href="https://t.me/catapulttrade_guide_bot">TG Bot</a> | 🐦 <a href="#">Twitter</a>"""

# ── Состояние ─────────────────────────────────────────────────────────────────
sent_hashes: set = set()
pending_posts: dict = {}      # посты ожидающие одобрения
approved_queue: dict = {}     # одобренные посты в очереди на публикацию
editing_post: dict = {}       # пост в режиме редактирования
catapult_angle_idx: int = 0   # текущий угол Catapult
poll_idx: int = 0             # текущий опрос

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

async def get_posts_web(channel: str) -> list:
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
            fresh = [m.get_text(separator="\n").strip() for m in msgs if len(m.get_text().strip()) > 100]
            for text in fresh[-3:]:
                h = make_hash(text)
                if h not in sent_hashes:
                    posts.append({"text": text, "channel": channel, "views": 0, "hash": h, "source": "web"})
    except Exception as e:
        logger.warning(f"Web error @{channel}: {e}")
    return posts

async def collect_top_posts(category: str) -> list:
    channels = CHANNELS.get(category, [])
    all_posts = []
    for channel in channels:
        posts = await get_posts_tgstat(channel)
        if not posts:
            posts = await get_posts_web(channel)
        all_posts.extend(posts)
        await asyncio.sleep(0.5)
    tgstat_posts = sorted([p for p in all_posts if p["source"] == "tgstat"], key=lambda x: x["views"], reverse=True)
    web_posts    = [p for p in all_posts if p["source"] == "web"]
    return (tgstat_posts + web_posts)[:TOP_POSTS]

# ── Claude API — генерация поста ──────────────────────────────────────────────
STYLE_GUIDE = """Ты — автор Telegram канала «Крипта, AI, Forex. Как заработать?».

Твой стиль:
- Начинаешь с 👋 Друзья, ... или 👋 Друзья, всем привет! или 👋 Друзья, приветствую!
- Каждый абзац начинается с тематического эмодзи
- Пишешь от первого лица, живо и практично
- Ключевые слова и цифры выделяешь <b>жирным</b>
- Иногда используешь <i>курсив</i> для пояснений
- Важные правила оформляешь через <blockquote>
- 150-250 слов
- В конце всегда призыв к действию
- НЕ копируешь дословно — пересказываешь своими словами"""

async def generate_post_claude(text: str, category: str) -> str:
    context = {
        "crypto": "криптовалюты, Bitcoin, блокчейн, DeFi, альткоины",
        "ai":     "искусственный интеллект, нейросети, AI инструменты для заработка",
        "forex":  "Forex, валютные пары, трейдинг, аналитика рынка"
    }
    prompt = f"""{STYLE_GUIDE}

Тема: {context.get(category, 'финансы')}

На основе этой новости напиши пост для канала с HTML форматированием (теги: <b>, <i>, <blockquote>, <a href="">):

{text[:1500]}

В конце добавь: 👉 Подробнее в боте: @catapulttrade_guide_bot

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

Напиши ТЗ в 2-3 предложения: что должно быть на картинке, цвета, настроение. Без лишних слов."""

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

# ── Отправка поста на одобрение ───────────────────────────────────────────────
async def send_for_approval(post_text: str, category: str, slot: str, source: str = "", original: str = ""):
    global catapult_angle_idx, poll_idx

    post_id = f"{slot}_{hashlib.md5(post_text[:50].encode()).hexdigest()[:8]}"

    # Генерируем ТЗ для картинки
    brief = await generate_image_brief(post_text, category)

    pending_posts[post_id] = {
        "text": post_text + CHANNEL_SIGNATURE,
        "original": original or post_text,
        "category": category,
        "slot": slot,
        "source": source,
        "brief": brief,
    }

    emoji_map = {
        "crypto":   "🪙 КРИПТА",
        "ai":       "🤖 ИИ",
        "forex":    "💹 ФОРЕКС",
        "catapult": "💰 CATAPULT TRADE",
        "poll":     "📊 ОПРОС",
    }
    label = emoji_map.get(category, category.upper())

    time_map = {
        "crypto_1":   "09:00",
        "catapult_1": "11:00",
        "ai":         "13:00",
        "catapult_2": "15:00",
        "poll":       "16:30",
        "forex":      "18:00",
        "crypto_2":   "20:00",
    }
    pub_time = time_map.get(slot, "??:??")

    preview = (
        f"📌 <b>{label}</b> | публикация завтра в {pub_time}\n"
        f"{'─' * 28}\n"
        f"{post_text[:600]}{'...' if len(post_text) > 600 else ''}\n"
        f"{'─' * 28}\n"
        f"🖼 <b>ТЗ для картинки:</b>\n{brief}"
    )

    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": preview,
                "parse_mode": "HTML",
                "reply_markup": approval_keyboard(post_id)
            }
        )

# ── Обработчики кнопок ────────────────────────────────────────────────────────
async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split("_", 1)
    action = parts[0]
    post_id = parts[1]
    post = pending_posts.get(post_id)

    if not post:
        await query.edit_message_text("⚠️ Пост не найден или уже обработан.")
        return

    if action == "approve":
        approved_queue[post["slot"]] = post
        await query.edit_message_text(
            f"✅ <b>Одобрено!</b> Пост встал в очередь.\n"
            f"Публикация: завтра по расписанию 🕐",
            parse_mode="HTML"
        )
        pending_posts.pop(post_id, None)

    elif action == "cancel":
        await query.edit_message_text("❌ Пост отменён.")
        pending_posts.pop(post_id, None)

    elif action == "edit":
        editing_post[ADMIN_TG_ID] = post_id
        await query.edit_message_text(
            f"✏️ <b>Режим редактирования</b>\n\n"
            f"Текущий текст:\n\n{post['text'][:800]}\n\n"
            f"Пришли исправленный текст ответным сообщением.\n"
            f"Для отмены: /cancel",
            parse_mode="HTML"
        )

    elif action == "rewrite":
        await query.edit_message_text("🔄 Переписываю...")
        try:
            category = post["category"]
            if category == "catapult":
                new_text = await generate_catapult_post(random.choice(CATAPULT_ANGLES))
            else:
                new_text = await generate_post_claude(post["original"], category)

            new_brief = await generate_image_brief(new_text, category)
            pending_posts[post_id]["text"] = new_text + CHANNEL_SIGNATURE
            pending_posts[post_id]["brief"] = new_brief

            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": ADMIN_TG_ID,
                        "text": (
                            f"🔄 <b>Новый вариант:</b>\n"
                            f"{'─' * 28}\n"
                            f"{new_text[:600]}\n"
                            f"{'─' * 28}\n"
                            f"🖼 <b>ТЗ для картинки:</b>\n{new_brief}"
                        ),
                        "parse_mode": "HTML",
                        "reply_markup": approval_keyboard(post_id)
                    }
                )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")

# ── Обработчик редактирования ─────────────────────────────────────────────────
async def handle_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_TG_ID:
        return

    if update.message.text == "/cancel":
        editing_post.pop(user_id, None)
        await update.message.reply_text("✅ Редактирование отменено.")
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
    posts = await collect_top_posts("crypto")
    if posts:
        text = await generate_post_claude(posts[0]["text"], "crypto")
        await send_for_approval(text, "crypto", "crypto_1", posts[0]["channel"], posts[0]["text"])
        await asyncio.sleep(2)

    # 2. Catapult #1 (11:00)
    angle1 = CATAPULT_ANGLES[catapult_angle_idx % len(CATAPULT_ANGLES)]
    catapult_angle_idx += 1
    text = await generate_catapult_post(angle1)
    await send_for_approval(text, "catapult", "catapult_1")
    await asyncio.sleep(2)

    # 3. ИИ (13:00)
    posts = await collect_top_posts("ai")
    if posts:
        text = await generate_post_claude(posts[0]["text"], "ai")
        await send_for_approval(text, "ai", "ai", posts[0]["channel"], posts[0]["text"])
        await asyncio.sleep(2)

    # 4. Catapult #2 (15:00)
    angle2 = CATAPULT_ANGLES[catapult_angle_idx % len(CATAPULT_ANGLES)]
    catapult_angle_idx += 1
    text = await generate_catapult_post(angle2)
    await send_for_approval(text, "catapult", "catapult_2")
    await asyncio.sleep(2)

    # 5. Опрос (16:30)
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

    # 6. Форекс (18:00)
    posts = await collect_top_posts("forex")
    if posts:
        text = await generate_post_claude(posts[0]["text"], "forex")
        await send_for_approval(text, "forex", "forex", posts[0]["channel"], posts[0]["text"])
        await asyncio.sleep(2)

    # 7. Крипта #2 (20:00)
    posts = await collect_top_posts("crypto")
    if len(posts) > 1:
        text = await generate_post_claude(posts[1]["text"], "crypto")
        await send_for_approval(text, "crypto", "crypto_2", posts[1]["channel"], posts[1]["text"])

    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": "✅ <b>Все посты готовы!</b>\n\nОдобри или отредактируй каждый — они опубликуются завтра автоматически по расписанию.",
                "parse_mode": "HTML"
            }
        )

# ── Автопубликация по расписанию ──────────────────────────────────────────────
async def auto_publish(slot: str):
    post = approved_queue.get(slot)
    if not post:
        logger.warning(f"Нет одобренного поста для слота {slot}")
        return

    logger.info(f"Публикую слот: {slot}")
    category = post["category"]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if category == "poll" and "poll_data" in post:
                poll_data = post["poll_data"]
                await client.post(
                    f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendPoll",
                    json={
                        "chat_id": CHANNEL_ID,
                        "question": poll_data["question"],
                        "options": poll_data["options"],
                        "is_anonymous": True,
                    }
                )
            else:
                await client.post(
                    f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": CHANNEL_ID,
                        "text": post["text"],
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True
                    }
                )
        approved_queue.pop(slot, None)
        logger.info(f"✅ Опубликовано: {slot}")
    except Exception as e:
        logger.error(f"Ошибка публикации {slot}: {e}")

# ── Воскресный контент-план ───────────────────────────────────────────────────
async def send_weekly_plan():
    logger.info("=== Воскресный контент-план ===")
    plan = await generate_weekly_plan()
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": f"📅 <b>КОНТЕНТ-ПЛАН НА НЕДЕЛЮ</b>\n\n{plan}",
                "parse_mode": "HTML"
            }
        )

# ── Запуск ────────────────────────────────────────────────────────────────────
async def main():
    app = Application.builder().token(PARSER_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_approval, pattern="^(approve|cancel|edit|rewrite)_"))
    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_TG_ID), handle_edit_message))

    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")

    # Вечерняя генерация каждый день в 20:00
    scheduler.add_job(evening_generation, "cron", hour=20, minute=0)

    # Воскресный контент-план в 19:00
    scheduler.add_job(send_weekly_plan, "cron", day_of_week="sun", hour=19, minute=0)

    # Автопубликация по слотам
    for s in PUBLISH_SCHEDULE:
        scheduler.add_job(
            auto_publish, "cron",
            hour=s["hour"], minute=s["minute"],
            args=[s["slot"]]
        )

    scheduler.start()
    logger.info("✅ Parser v7 запущен!")
    logger.info("📅 Генерация: каждый день в 20:00")
    logger.info("📊 Контент-план: воскресенье 19:00")
    logger.info("📢 Публикации: 09:00 / 11:00 / 13:00 / 15:00 / 16:30 / 18:00 / 20:00")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
