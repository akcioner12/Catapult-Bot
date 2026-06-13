"""
Автопарсер контента v2
- Берёт 2 последних поста из КАЖДОГО канала категории
- Не дублирует уже отправленные посты
- Переписывает через Claude
- Отправляет на одобрение по расписанию
"""

import os
import asyncio
import logging
import random
import hashlib

import httpx
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Конфиг ────────────────────────────────────────────────────────────────────
PARSER_BOT_TOKEN = os.getenv("PARSER_BOT_TOKEN")
ADMIN_TG_ID      = int(os.getenv("ADMIN_TG_ID", "0"))
CHANNEL_ID       = os.getenv("CHANNEL_ID", "@Crypto_AI_Forex")
CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY", "")
MAIN_BOT_TOKEN   = os.getenv("BOT_TOKEN")
CLAUDE_API_URL   = "https://api.anthropic.com/v1/messages"

# ── Каналы ────────────────────────────────────────────────────────────────────
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

# Расписание (UTC — Киев UTC+3)
SCHEDULE_TIMES = [
    {"hour": 6,  "minute": 0,  "category": "crypto"},
    {"hour": 10, "minute": 0,  "category": "ai"},
    {"hour": 14, "minute": 0,  "category": "forex"},
    {"hour": 17, "minute": 0,  "category": "crypto"},
]

# Хранилище уже отправленных постов (хэши текстов)
sent_hashes: set = set()
pending_posts = {}

CHANNEL_SIGNATURE = """

———
🔔 [Подпишись на соцсети и не пропусти важное]()

▶️ [YouTube]() | 💬 [TG Chat]() | 🎵 [TikTok]() | 📷 [Instagram]() | 🤖 [TG Bot](https://t.me/catapulttrade_guide_bot) | 🐦 [Twitter]()"""

# ── Парсинг ───────────────────────────────────────────────────────────────────

def text_hash(text: str) -> str:
    """Хэш текста для проверки дублей"""
    return hashlib.md5(text[:200].encode()).hexdigest()

async def fetch_channel_posts(channel: str, limit: int = 2) -> list:
    """Берёт последние N постов из канала"""
    posts = []
    url = f"https://t.me/s/{channel}"
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=15
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning(f"Cannot fetch {channel}: {resp.status_code}")
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            messages = soup.find_all("div", class_="tgme_widget_message_text")

            # Берём последние посты (конец списка — самые свежие)
            fresh = [m for m in messages if len(m.get_text().strip()) > 80]
            for msg in fresh[-limit:]:
                text = msg.get_text(separator="\n").strip()
                h = text_hash(text)
                if h not in sent_hashes:
                    posts.append({"text": text, "channel": channel, "hash": h})

    except Exception as e:
        logger.warning(f"Error fetching {channel}: {e}")
    return posts

async def get_all_posts(category: str) -> list:
    """Собирает по 2 свежих поста из КАЖДОГО канала категории"""
    channels = CHANNELS.get(category, [])
    all_posts = []

    for channel in channels:
        posts = await fetch_channel_posts(channel, limit=2)
        if posts:
            all_posts.extend(posts)
            logger.info(f"Got {len(posts)} posts from @{channel}")
        await asyncio.sleep(0.5)  # небольшая пауза между запросами

    logger.info(f"Total posts for {category}: {len(all_posts)}")
    return all_posts

# ── Claude API ────────────────────────────────────────────────────────────────

async def rewrite_with_claude(text: str, category: str) -> str:
    context = {
        "crypto": "криптовалюты, Bitcoin, блокчейн, DeFi",
        "ai":     "искусственный интеллект, нейросети, AI инструменты",
        "forex":  "Forex, валютные пары, трейдинг"
    }
    prompt = f"""Ты — автор Telegram канала о {context.get(category, 'финансах')}.

Перепиши пост в стиле канала:
1. Начни с 👋 Друзья, ...
2. Каждый абзац — с тематическим эмодзи
3. Ключевые мысли — **жирным**
4. Второстепенное — _курсивом_
5. Важные правила — цитатой (> текст)
6. В конце — призыв к @catapulttrade_guide_bot
7. Живо, от первого лица, 150-250 слов
8. НЕ копируй дословно

Оригинал:
{text[:1500]}

Только готовый пост."""

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
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            return resp.json()["content"][0]["text"]
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return text

# ── Отправка на одобрение ─────────────────────────────────────────────────────

async def send_post_for_approval(app: Application, post: dict, category: str, index: int, total: int):
    """Отправляет один пост на одобрение"""
    post_id = f"{category}_{len(pending_posts)}"
    rewritten = await rewrite_with_claude(post["text"], category)

    pending_posts[post_id] = {
        "text": rewritten + CHANNEL_SIGNATURE,
        "original": post["text"],
        "category": category,
        "source": post["channel"],
        "hash": post["hash"]
    }

    emoji = {"crypto": "📈", "ai": "🤖", "forex": "💹"}.get(category, "📌")
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve_{post_id}"),
            InlineKeyboardButton("❌ Пропустить",   callback_data=f"reject_{post_id}")
        ],
        [InlineKeyboardButton("🔄 Переписать заново", callback_data=f"next_{post_id}")]
    ])

    preview = (
        f"{emoji} *Пост {index}/{total} [{category.upper()}]*\n"
        f"📡 @{post['channel']}\n\n"
        f"{'─'*20}\n"
        f"{rewritten[:600]}{'...' if len(rewritten) > 600 else ''}\n"
        f"{'─'*20}"
    )

    await app.bot.send_message(
        chat_id=ADMIN_TG_ID,
        text=preview,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await asyncio.sleep(1)  # пауза между сообщениями

# ── Обработчики кнопок ────────────────────────────────────────────────────────

async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, post_id = query.data.split("_", 1)
    post = pending_posts.get(post_id)

    if not post:
        await query.edit_message_text("⚠️ Пост не найден.")
        return

    if action == "approve":
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": CHANNEL_ID,
                        "text": post["text"],
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True
                    }
                )
            # Добавляем хэш в отправленные чтобы не дублировать
            sent_hashes.add(post["hash"])
            await query.edit_message_text(f"✅ Опубликовано в {CHANNEL_ID}!")
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")
        pending_posts.pop(post_id, None)

    elif action == "reject":
        # При отклонении тоже добавляем хэш чтобы не показывать снова
        sent_hashes.add(post["hash"])
        await query.edit_message_text("❌ Пропущено.")
        pending_posts.pop(post_id, None)

    elif action == "next":
        await query.edit_message_text("🔄 Переписываю...")
        new_text = await rewrite_with_claude(post["original"], post["category"])
        pending_posts[post_id]["text"] = new_text + CHANNEL_SIGNATURE

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve_{post_id}"),
                InlineKeyboardButton("❌ Пропустить",   callback_data=f"reject_{post_id}")
            ],
            [InlineKeyboardButton("🔄 Переписать заново", callback_data=f"next_{post_id}")]
        ])
        await context.bot.send_message(
            chat_id=ADMIN_TG_ID,
            text=f"🔄 Новый вариант:\n\n{new_text[:800]}",
            reply_markup=keyboard
        )

# ── Планировщик ───────────────────────────────────────────────────────────────

async def scheduled_task(app: Application, category: str):
    logger.info(f"Scheduled parse: {category}")

    posts = await get_all_posts(category)

    if not posts:
        logger.warning(f"No posts found for {category}")
        await app.bot.send_message(
            chat_id=ADMIN_TG_ID,
            text=f"⚠️ Нет новых постов по теме {category.upper()}"
        )
        return

    await app.bot.send_message(
        chat_id=ADMIN_TG_ID,
        text=f"📬 Найдено *{len(posts)} постов* по теме *{category.upper()}*. Отправляю на одобрение...",
        parse_mode="Markdown"
    )

    for i, post in enumerate(posts, 1):
        await send_post_for_approval(app, post, category, i, len(posts))
        await asyncio.sleep(2)

# ── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    app = Application.builder().token(PARSER_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_approval, pattern="^(approve|reject|next)_"))

    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    for s in SCHEDULE_TIMES:
        scheduler.add_job(
            scheduled_task, "cron",
            hour=s["hour"], minute=s["minute"],
            args=[app, s["category"]]
        )
    scheduler.start()
    logger.info("Parser v2 started! Posts at 9:00, 13:00, 17:00, 20:00 Kyiv")

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
