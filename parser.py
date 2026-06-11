"""
Автопарсер контента для ТГ канала
- Парсит посты из тематических каналов
- Переписывает через Claude API в стиль канала
- Отправляет на одобрение в 9:00, 13:00, 17:00, 20:00
- Публикует в канал после одобрения
"""

import os
import asyncio
import logging
import json
import random
from datetime import datetime, time
from typing import Optional

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Конфиг ────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN")
ADMIN_TG_ID     = int(os.getenv("ADMIN_TG_ID", "0"))   # твой Telegram ID
CHANNEL_ID      = os.getenv("CHANNEL_ID", "@Crypto_AI_Forex")
CLAUDE_API_KEY  = os.getenv("CLAUDE_API_KEY", "")

# Telethon (для чтения каналов)
TG_API_ID       = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH     = os.getenv("TG_API_HASH", "")
TG_PHONE        = os.getenv("TG_PHONE", "")

CLAUDE_API_URL  = "https://api.anthropic.com/v1/messages"

# ── Каналы для мониторинга ────────────────────────────────────────────────────
CHANNELS = {
    "crypto": [
        "crypto_Iemon", "to_the_makemoney", "cryptocurrencyfore_dumbs",
        "vs_cryptokings", "airolejon", "krisspump", "eeusd",
        "if_crypto_ru", "cryptomedwed", "bitochekvko", "cryptanci",
        "DeCenter", "cointelegraph"
    ],
    "ai": [
        "web3nity_channel", "neurobussines", "naebnet",
        "neyroseti_dr", "loading100ai"
    ],
    "forex": [
        "Delayprofit", "PROFiInvest", "tradeforexexchange",
        "premiumgolubev", "markoptions", "newwavetrade",
        "goldenonemoney", "uiartemzvezdin"
    ]
}

# Расписание публикаций (UTC+3 Киев)
SCHEDULE_TIMES = [
    {"hour": 6,  "minute": 0,  "category": "crypto"},   # 9:00 Киев
    {"hour": 10, "minute": 0,  "category": "ai"},        # 13:00 Киев
    {"hour": 14, "minute": 0,  "category": "forex"},     # 17:00 Киев
    {"hour": 17, "minute": 0,  "category": "crypto"},    # 20:00 Киев
]

# Хранилище ожидающих постов { message_id: post_data }
pending_posts = {}

# ── Подпись канала ─────────────────────────────────────────────────────────────
CHANNEL_SIGNATURE = """

———
🔔 [Подпишись на соцсети и не пропусти важное]()

▶️ [YouTube]() | 💬 [TG Chat]() | 🎵 [TikTok]() | 📷 [Instagram]() | 🤖 [TG Bot](https://t.me/catapulttrade_guide_bot) | 🐦 [Twitter]()"""

# ── Claude API ────────────────────────────────────────────────────────────────

async def rewrite_with_claude(original_text: str, category: str) -> str:
    """Переписывает пост через Claude API в стиль канала"""

    category_context = {
        "crypto": "криптовалюты, Bitcoin, альткоины, DeFi, блокчейн",
        "ai": "искусственный интеллект, нейросети, AI инструменты, автоматизация",
        "forex": "Forex, валютные пары, трейдинг, технический анализ"
    }

    prompt = f"""Ты — автор Telegram канала о {category_context.get(category, 'финансах')}.

Перепиши следующий пост в стиле канала. Правила:
1. Начни с приветствия с эмодзи (👋 Друзья, и т.п.)
2. Используй эмодзи в начале каждого абзаца
3. Ключевые мысли выдели **жирным**
4. Второстепенное — _курсивом_
5. Цитаты или важные правила — оформи как цитату (> текст)
6. В конце добавь призыв подписаться или перейти в бот
7. Пиши живо, от первого лица, как будто сам торгуешь
8. Упомяни @catapulttrade_guide_bot если уместно
9. Длина — 150-250 слов
10. НЕ копируй оригинал дословно — перескажи своими словами

Оригинальный пост:
{original_text}

Напиши только готовый пост, без пояснений."""

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=30
            )
            data = response.json()
            return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return original_text

# ── Telethon парсер ───────────────────────────────────────────────────────────

async def get_recent_posts(client: TelegramClient, channel: str, limit: int = 20) -> list:
    """Получает последние посты из канала"""
    posts = []
    try:
        async for message in client.iter_messages(channel, limit=limit):
            if message.text and len(message.text) > 100:
                posts.append({
                    "text": message.text,
                    "views": getattr(message, "views", 0) or 0,
                    "date": message.date,
                    "channel": channel
                })
    except Exception as e:
        logger.warning(f"Cannot parse {channel}: {e}")
    return posts

async def get_best_post(client: TelegramClient, category: str) -> Optional[dict]:
    """Берёт лучший пост из категории по количеству просмотров"""
    channels = CHANNELS.get(category, [])
    random.shuffle(channels)  # Рандомизируем чтобы не брать всегда из одного

    all_posts = []
    for channel in channels[:5]:  # Берём из 5 случайных каналов
        posts = await get_recent_posts(client, channel, limit=10)
        all_posts.extend(posts)

    if not all_posts:
        return None

    # Сортируем по просмотрам и берём топ
    all_posts.sort(key=lambda x: x["views"], reverse=True)
    return all_posts[0] if all_posts else None

# ── Отправка на одобрение ─────────────────────────────────────────────────────

async def send_for_approval(app: Application, post_text: str, category: str, source_channel: str):
    """Отправляет пост админу на одобрение"""

    # Кнопки одобрения
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve_{len(pending_posts)}"),
            InlineKeyboardButton("❌ Пропустить",   callback_data=f"reject_{len(pending_posts)}")
        ],
        [
            InlineKeyboardButton("✏️ Следующий вариант", callback_data=f"next_{len(pending_posts)}")
        ]
    ])

    category_emoji = {"crypto": "📈", "ai": "🤖", "forex": "💹"}
    emoji = category_emoji.get(category, "📌")

    preview = f"""
{emoji} **Новый пост [{category.upper()}]**
📡 Источник: @{source_channel}

─────────────────
{post_text[:800]}{'...' if len(post_text) > 800 else ''}
─────────────────

Одобрить публикацию?
    """.strip()

    msg = await app.bot.send_message(
        chat_id=ADMIN_TG_ID,
        text=preview,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

    # Сохраняем в ожидающие
    post_id = str(len(pending_posts))
    pending_posts[post_id] = {
        "text": post_text + CHANNEL_SIGNATURE,
        "category": category,
        "source": source_channel,
        "msg_id": msg.message_id
    }

# ── Обработчики кнопок ────────────────────────────────────────────────────────

async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие кнопок одобрения/отклонения"""
    query = update.callback_query
    await query.answer()

    action, post_id = query.data.rsplit("_", 1)
    post = pending_posts.get(post_id)

    if not post:
        await query.edit_message_text("⚠️ Пост не найден или уже обработан.")
        return

    if action == "approve":
        # Публикуем в канал
        try:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=post["text"],
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            await query.edit_message_text(f"✅ Опубликовано в {CHANNEL_ID}!")
            logger.info(f"Post published to {CHANNEL_ID}")
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка публикации: {e}")

    elif action == "reject":
        await query.edit_message_text("❌ Пост пропущен.")

    elif action == "next":
        await query.edit_message_text("🔄 Генерирую новый вариант...")
        # Перегенерируем пост
        new_text = await rewrite_with_claude(post["text"], post["category"])
        pending_posts[post_id]["text"] = new_text + CHANNEL_SIGNATURE

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve_{post_id}"),
                InlineKeyboardButton("❌ Пропустить",   callback_data=f"reject_{post_id}")
            ],
            [
                InlineKeyboardButton("✏️ Следующий вариант", callback_data=f"next_{post_id}")
            ]
        ])
        await context.bot.send_message(
            chat_id=ADMIN_TG_ID,
            text=new_text[:800],
            reply_markup=keyboard
        )

    # Удаляем из ожидающих
    if action in ("approve", "reject"):
        pending_posts.pop(post_id, None)

# ── Планировщик ───────────────────────────────────────────────────────────────

async def scheduled_parse(app: Application, telethon_client: TelegramClient, category: str):
    """Запускается по расписанию — парсит и отправляет на одобрение"""
    logger.info(f"Scheduled parse: {category}")

    post = await get_best_post(telethon_client, category)
    if not post:
        logger.warning(f"No posts found for {category}")
        return

    # Переписываем через Claude
    rewritten = await rewrite_with_claude(post["text"], category)

    # Отправляем на одобрение
    await send_for_approval(app, rewritten, category, post["channel"])

# ── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    # Telethon клиент для чтения каналов
    telethon_client = TelegramClient("parser_session", TG_API_ID, TG_API_HASH)
    await telethon_client.start(phone=TG_PHONE)
    logger.info("Telethon client started")

    # Telegram бот
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_approval, pattern="^(approve|reject|next)_"))

    # Планировщик
    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    for schedule in SCHEDULE_TIMES:
        scheduler.add_job(
            scheduled_parse,
            "cron",
            hour=schedule["hour"],
            minute=schedule["minute"],
            args=[app, telethon_client, schedule["category"]]
        )
    scheduler.start()
    logger.info("Scheduler started — posts at 9:00, 13:00, 17:00, 20:00 Kyiv time")

    # Запускаем бота
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logger.info("Parser bot running!")

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await telethon_client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
