"""
Автопарсер контента v5
- Комбинированный: TGStat API + t.me/s/ как fallback
- Топ-5 по просмотрам (TGStat) или по длине (fallback)
- Переписывает через Claude
- Отправляет на одобрение
"""

import os
import asyncio
import logging
import hashlib

import httpx
from bs4 import BeautifulSoup
from telegram import Update
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
TGSTAT_TOKEN     = os.getenv("TGSTAT_TOKEN", "")
CLAUDE_API_URL   = "https://api.anthropic.com/v1/messages"
TGSTAT_API_URL   = "https://api.tgstat.ru"
TOP_POSTS        = 5

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

SCHEDULE_TIMES = [
    {"hour": 6,  "minute": 0,  "category": "crypto"},
    {"hour": 10, "minute": 0,  "category": "ai"},
    {"hour": 14, "minute": 0,  "category": "forex"},
    {"hour": 17, "minute": 0,  "category": "crypto"},
]

sent_hashes: set = set()
pending_posts: dict = {}

CHANNEL_SIGNATURE = """

———
🔔 [Подпишись на соцсети и не пропусти важное]()

▶️ [YouTube]() | 💬 [TG Chat]() | 🎵 [TikTok]() | 📷 [Instagram]() | 🤖 [TG Bot](https://t.me/catapulttrade_guide_bot) | 🐦 [Twitter]()"""

# ── Хэш ───────────────────────────────────────────────────────────────────────

def make_hash(text: str) -> str:
    return hashlib.md5(text[:200].encode()).hexdigest()

# ── TGStat API ────────────────────────────────────────────────────────────────

async def get_posts_tgstat(channel: str) -> list:
    """Получает посты через TGStat API"""
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

# ── t.me/s/ fallback ──────────────────────────────────────────────────────────

async def get_posts_web(channel: str) -> list:
    """Получает посты через t.me/s/ как запасной вариант"""
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

# ── Сбор топ постов ───────────────────────────────────────────────────────────

async def collect_top_posts(category: str) -> list:
    channels = CHANNELS.get(category, [])
    all_posts = []
    tgstat_count = 0
    web_count = 0

    for channel in channels:
        # Сначала пробуем TGStat
        posts = await get_posts_tgstat(channel)
        if posts:
            tgstat_count += len(posts)
            all_posts.extend(posts)
            logger.info(f"TGStat @{channel}: {len(posts)} posts")
        else:
            # Fallback на t.me/s/
            posts = await get_posts_web(channel)
            if posts:
                web_count += len(posts)
                all_posts.extend(posts)
                logger.info(f"Web @{channel}: {len(posts)} posts")
            else:
                logger.warning(f"No posts @{channel}")
        await asyncio.sleep(0.5)

    logger.info(f"Total {category}: {len(all_posts)} (TGStat: {tgstat_count}, Web: {web_count})")

    # Сортируем: с просмотрами вверху, остальные по длине
    tgstat_posts = [p for p in all_posts if p["source"] == "tgstat"]
    web_posts    = [p for p in all_posts if p["source"] == "web"]

    tgstat_posts.sort(key=lambda x: x["views"], reverse=True)
    web_posts.sort(key=lambda x: len(x["text"]), reverse=True)

    # Берём сначала из TGStat, потом добиваем из web
    combined = tgstat_posts + web_posts
    return combined[:TOP_POSTS]

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
            data = resp.json()
            if "content" in data:
                return data["content"][0]["text"]
            logger.error(f"Claude unexpected: {data}")
            return text
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return text

# ── Отправка на одобрение ─────────────────────────────────────────────────────

async def send_for_approval(post: dict, category: str, idx: int, total: int):
    post_id = f"{category}_{idx}_{len(pending_posts)}"

    try:
        rewritten = await rewrite_with_claude(post["text"], category)
    except Exception as e:
        logger.error(f"Rewrite failed: {e}")
        rewritten = post["text"]

    pending_posts[post_id] = {
        "text": rewritten + CHANNEL_SIGNATURE,
        "original": post["text"],
        "category": category,
        "source": post["channel"],
        "hash": post["hash"],
        "views": post.get("views", 0)
    }

    emoji = {"crypto": "📈", "ai": "🤖", "forex": "💹"}.get(category, "📌")
    views = post.get("views", 0)
    views_str = f"👁 {views:,}" if views > 0 else "📡 web"

    preview = (
        f"{emoji} *Пост {idx}/{total} — {category.upper()}*\n"
        f"📡 @{post['channel']} | {views_str}\n\n"
        f"{'─'*20}\n"
        f"{rewritten[:500]}{'...' if len(rewritten) > 500 else ''}\n"
        f"{'─'*20}"
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"approve_{post_id}"},
            {"text": "❌ Пропустить",   "callback_data": f"reject_{post_id}"}
        ], [
            {"text": "🔄 Переписать", "callback_data": f"next_{post_id}"}
        ]]
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": preview,
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            }
        )
        if r.status_code != 200:
            logger.error(f"Send error: {r.text}")

# ── Обработчики кнопок ────────────────────────────────────────────────────────

async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_", 1)
    action = parts[0]
    post_id = parts[1]
    post = pending_posts.get(post_id)

    if not post:
        await query.edit_message_text("⚠️ Пост не найден или уже обработан.")
        return

    if action == "approve":
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": CHANNEL_ID,
                        "text": post["text"],
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True
                    }
                )
                if r.status_code == 200:
                    sent_hashes.add(post["hash"])
                    await query.edit_message_text(f"✅ Опубликовано в {CHANNEL_ID}!")
                else:
                    await query.edit_message_text(f"❌ Ошибка: {r.text[:200]}")
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")
        pending_posts.pop(post_id, None)

    elif action == "reject":
        sent_hashes.add(post["hash"])
        await query.edit_message_text("❌ Пропущено.")
        pending_posts.pop(post_id, None)

    elif action == "next":
        await query.edit_message_text("🔄 Переписываю...")
        try:
            new_text = await rewrite_with_claude(post["original"], post["category"])
            pending_posts[post_id]["text"] = new_text + CHANNEL_SIGNATURE
            keyboard = {
                "inline_keyboard": [[
                    {"text": "✅ Опубликовать", "callback_data": f"approve_{post_id}"},
                    {"text": "❌ Пропустить",   "callback_data": f"reject_{post_id}"}
                ], [
                    {"text": "🔄 Переписать", "callback_data": f"next_{post_id}"}
                ]]
            }
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": ADMIN_TG_ID,
                        "text": f"🔄 Новый вариант:\n\n{new_text[:600]}",
                        "parse_mode": "Markdown",
                        "reply_markup": keyboard
                    }
                )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")

# ── Планировщик ───────────────────────────────────────────────────────────────

async def scheduled_task(app: Application, category: str):
    logger.info(f"=== Scheduled: {category} ===")

    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": f"🔍 Собираю топ посты по *{category.upper()}*...",
                "parse_mode": "Markdown"
            }
        )

    posts = await collect_top_posts(category)

    if not posts:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                json={"chat_id": ADMIN_TG_ID, "text": f"⚠️ Нет новых постов по {category.upper()}"}
            )
        return

    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": f"📬 Топ *{len(posts)}* постов по *{category.upper()}*. Отправляю...",
                "parse_mode": "Markdown"
            }
        )

    for i, post in enumerate(posts, 1):
        try:
            await send_for_approval(post, category, i, len(posts))
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Error sending post {i}: {e}")

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
    logger.info("Parser v5 started! 9:00 / 13:00 / 17:00 / 20:00 Kyiv")

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
