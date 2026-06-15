"""
Автопарсер контента v6
- Комбинированный: TGStat API + t.me/s/ как fallback
- HTML форматирование (жирный, курсив, ссылки)
- Кнопка Редактировать перед публикацией
- Генерация картинки через DALL-E
- Топ-5 по просмотрам
"""

import os
import asyncio
import logging
import hashlib

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
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
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
editing_post: dict = {}  # Хранит post_id для которого ждём редактирование

CHANNEL_SIGNATURE = """

———
🔔 <a href="#">Подпишись на соцсети и не пропусти важное</a>

▶️ <a href="#">YouTube</a> | 💬 <a href="#">TG Chat</a> | 🎵 <a href="#">TikTok</a> | 📷 <a href="#">Instagram</a> | 🤖 <a href="https://t.me/catapulttrade_guide_bot">TG Bot</a> | 🐦 <a href="#">Twitter</a>"""

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
    web_posts    = sorted([p for p in all_posts if p["source"] == "web"], key=lambda x: len(x["text"]), reverse=True)

    return (tgstat_posts + web_posts)[:TOP_POSTS]

# ── Claude API ────────────────────────────────────────────────────────────────

async def rewrite_with_claude(text: str, category: str) -> str:
    context = {
        "crypto": "криптовалюты, Bitcoin, блокчейн, DeFi",
        "ai":     "искусственный интеллект, нейросети, AI инструменты",
        "forex":  "Forex, валютные пары, трейдинг"
    }
    prompt = f"""Ты — автор Telegram канала о {context.get(category, 'финансах')}.

Перепиши пост используя HTML форматирование для Telegram:
- <b>жирный текст</b> для ключевых мыслей
- <i>курсив</i> для второстепенного
- <a href="url">ссылка</a> для ссылок
- <blockquote>цитата</blockquote> для важных правил

Правила написания:
1. Начни с 👋 <b>Друзья,</b> ...
2. Каждый абзац начинай с тематического эмодзи
3. Ключевые факты и цифры — всегда <b>жирным</b>
4. 150-250 слов
5. В конце призыв: "Подробнее узнай в боте 👉 @catapulttrade_guide_bot"
6. НЕ копируй дословно — перескажи своими словами
7. Пиши живо, от первого лица

Оригинал:
{text[:1500]}

Только готовый пост с HTML тегами, без пояснений."""

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
            return text
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return text

# ── DALL-E картинка ───────────────────────────────────────────────────────────

async def generate_image(text: str, category: str) -> str | None:
    """Генерирует картинку через DALL-E 3 и возвращает URL"""
    if not OPENAI_API_KEY:
        return None

    prompts = {
        "crypto": "Futuristic cryptocurrency Bitcoin trading chart dark background neon blue orange glow cinematic photorealistic",
        "ai":     "Artificial intelligence neural network glowing circuits dark background purple cyan neon cinematic photorealistic",
        "forex":  "Forex trading currency pairs bull market dark background green blue neon charts cinematic photorealistic"
    }
    image_prompt = prompts.get(category, prompts["crypto"])

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/images/generations",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "dall-e-3",
                    "prompt": image_prompt,
                    "n": 1,
                    "size": "1792x1024",
                    "quality": "standard"
                }
            )
            data = resp.json()
            if "data" in data and len(data["data"]) > 0:
                return data["data"][0]["url"]
            logger.error(f"DALL-E response: {data}")
            return None
    except Exception as e:
        logger.error(f"DALL-E error: {e}")
        return None

# ── Клавиатура ────────────────────────────────────────────────────────────────

def approval_keyboard(post_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"approve_{post_id}"},
            {"text": "❌ Пропустить",   "callback_data": f"reject_{post_id}"}
        ], [
            {"text": "✏️ Редактировать", "callback_data": f"edit_{post_id}"},
            {"text": "🔄 Переписать",    "callback_data": f"next_{post_id}"}
        ]]
    }

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
        f"{emoji} <b>Пост {idx}/{total} — {category.upper()}</b>\n"
        f"📡 @{post['channel']} | {views_str}\n\n"
        f"{'─'*20}\n"
        f"{rewritten[:500]}{'...' if len(rewritten) > 500 else ''}\n"
        f"{'─'*20}"
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

    parts = query.data.split("_", 1)
    action = parts[0]
    post_id = parts[1]
    post = pending_posts.get(post_id)

    if not post:
        await query.edit_message_text("⚠️ Пост не найден или уже обработан.")
        return

    if action == "approve":
        # Генерируем картинку если есть OpenAI ключ
        image_url = await generate_image(post["text"], post["category"])

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                if image_url:
                    # Публикуем с картинкой
                    r = await client.post(
                        f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendPhoto",
                        json={
                            "chat_id": CHANNEL_ID,
                            "photo": image_url,
                            "caption": post["text"],
                            "parse_mode": "HTML"
                        }
                    )
                else:
                    # Публикуем без картинки
                    r = await client.post(
                        f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": CHANNEL_ID,
                            "text": post["text"],
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True
                        }
                    )
                if r.status_code == 200:
                    sent_hashes.add(post["hash"])
                    await query.edit_message_text("✅ Опубликовано!")
                else:
                    await query.edit_message_text(f"❌ Ошибка: {r.text[:200]}")
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")
        pending_posts.pop(post_id, None)

    elif action == "reject":
        sent_hashes.add(post["hash"])
        await query.edit_message_text("❌ Пропущено.")
        pending_posts.pop(post_id, None)

    elif action == "edit":
        # Запоминаем что ждём редактирование для этого поста
        editing_post[ADMIN_TG_ID] = post_id
        await query.edit_message_text(
            f"✏️ <b>Режим редактирования</b>\n\n"
            f"Текущий текст:\n\n{post['text'][:800]}\n\n"
            f"Пришли исправленный текст в ответном сообщении.\n"
            f"Для отмены напиши /cancel",
            parse_mode="HTML"
        )

    elif action == "next":
        await query.edit_message_text("🔄 Переписываю...")
        try:
            new_text = await rewrite_with_claude(post["original"], post["category"])
            pending_posts[post_id]["text"] = new_text + CHANNEL_SIGNATURE

            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": ADMIN_TG_ID,
                        "text": f"🔄 <b>Новый вариант:</b>\n\n{new_text[:600]}",
                        "parse_mode": "HTML",
                        "reply_markup": approval_keyboard(post_id)
                    }
                )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")

# ── Обработчик редактирования ─────────────────────────────────────────────────

async def handle_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает отредактированный текст от пользователя"""
    user_id = update.effective_user.id

    if user_id != ADMIN_TG_ID:
        return

    # Проверяем отмену
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

    # Обновляем текст
    new_text = update.message.text
    pending_posts[post_id]["text"] = new_text + CHANNEL_SIGNATURE
    editing_post.pop(user_id, None)

    # Показываем обновлённый вариант
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": f"✅ <b>Текст обновлён!</b>\n\n{new_text[:600]}",
                "parse_mode": "HTML",
                "reply_markup": approval_keyboard(post_id)
            }
        )

# ── Планировщик ───────────────────────────────────────────────────────────────

async def scheduled_task(app: Application, category: str):
    logger.info(f"=== Scheduled: {category} ===")

    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": f"🔍 Собираю топ посты по <b>{category.upper()}</b>...",
                "parse_mode": "HTML"
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
                "text": f"📬 Топ <b>{len(posts)}</b> постов по <b>{category.upper()}</b>. Отправляю...",
                "parse_mode": "HTML"
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
    app.add_handler(CallbackQueryHandler(handle_approval, pattern="^(approve|reject|edit|next)_"))
    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_TG_ID), handle_edit_message))

    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    for s in SCHEDULE_TIMES:
        scheduler.add_job(
            scheduled_task, "cron",
            hour=s["hour"], minute=s["minute"],
            args=[app, s["category"]]
        )
    scheduler.start()
    logger.info("Parser v6 started! 9:00 / 13:00 / 17:00 / 20:00 Kyiv")

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
