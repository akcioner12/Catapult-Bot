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
import hashlib

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
    save_pending()

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
        f"{post_text}\n"
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

# ── Обработчики кнопок (модерация постов — только админ) ─────────────────────
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
        # Переносим в pending_approval — ждём фото
        awaiting_photo[ADMIN_TG_ID] = post_id
        pending_posts[post_id]["approved"] = True
        await query.edit_message_text(
            f"✅ <b>Пост одобрен!</b>\n\n"
            f"📎 Прикрепи картинку к посту или нажми кнопку ниже.",
            parse_mode="HTML",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "⏭ Пропустить картинку", "callback_data": f"skipphoto_{post_id}"}
                ]]
            }
        )

    elif action == "skipphoto":
        awaiting_photo.pop(ADMIN_TG_ID, None)
        approved_queue[post["slot"]] = post
        save_approved()
        await query.edit_message_text(
            f"✅ <b>Одобрено без картинки!</b> Пост встал в очередь.\n"
            f"Публикация: завтра по расписанию 🕐",
            parse_mode="HTML"
        )
        pending_posts.pop(post_id, None)
        save_pending()

    elif action == "cancel":
        await query.edit_message_text("❌ Пост отменён.")
        pending_posts.pop(post_id, None)
        save_pending()

    elif action == "edit":
        editing_post[ADMIN_TG_ID] = post_id
        # Меняем сообщение — показываем статус ожидания
        await query.edit_message_text(
            f"✏️ <b>Режим редактирования</b>\n"
            f"Пришли исправленный текст. Для отмены: /cancel",
            parse_mode="HTML"
        )
        # Отправляем полный текст отдельным сообщением для копирования
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": ADMIN_TG_ID,
                    "text": (
                        f"📋 <b>Полный текст поста — скопируй и отредактируй:</b>\n"
                        f"{'─' * 28}\n\n"
                        f"{post['text']}"
                    ),
                    "parse_mode": "HTML",
                }
            )

    elif action == "rewrite":
        await query.edit_message_text("🔄 Переписываю...")
        try:
            category = post["category"]
            if category == "catapult":
                new_text = await generate_catapult_post(random.choice(CATAPULT_ANGLES))
            else:
                # При переписывании используем оригинальный текст как единственный источник
                new_text = await generate_post_claude([{"text": post["original"], "channel": "original", "views": 0}], category)

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

# ── Обработчик фото (только админ) ────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_TG_ID:
        return

    post_id = awaiting_photo.get(user_id)
    if not post_id:
        return

    post = pending_posts.get(post_id)
    if not post:
        awaiting_photo.pop(user_id, None)
        return

    await update.message.reply_text("⏳ Скачиваю картинку на сервер...")

    try:
        # Получаем file_id максимального разрешения
        photo = update.message.photo[-1]
        file_id = photo.file_id

        # Получаем путь к файлу через Bot API
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/getFile",
                params={"file_id": file_id}
            )
            file_data = resp.json()
            file_path = file_data["result"]["file_path"]

            # Скачиваем файл на сервер
            file_resp = await client.get(
                f"https://api.telegram.org/file/bot{PARSER_BOT_TOKEN}/{file_path}"
            )

            # Сохраняем на диск
            local_path = f"{PHOTOS_DIR}/{post_id}.jpg"
            with open(local_path, "wb") as f:
                f.write(file_resp.content)

        # Сохраняем путь к файлу (не file_id!)
        pending_posts[post_id]["photo_path"] = local_path
        awaiting_photo.pop(user_id, None)

        # Одобряем пост
        approved_queue[post["slot"]] = pending_posts[post_id]
        save_approved()
        pending_posts.pop(post_id, None)
        save_pending()

        await update.message.reply_text(
            "✅ <b>Картинка сохранена на сервер! Пост встал в очередь.</b>\n"
            "Публикация: завтра по расписанию 🕐",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Photo download error: {e}")
        await update.message.reply_text(f"❌ Ошибка при сохранении картинки: {e}")

# ── Обработчик редактирования (только админ) ──────────────────────────────────
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
            elif post.get("photo_path") and os.path.exists(post["photo_path"]):
                # Сначала фото без текста, потом полный текст с футером
                from telegram import Bot, InputFile
                main_bot = Bot(token=MAIN_BOT_TOKEN)
                try:
                    with open(post["photo_path"], "rb") as photo_file:
                        await main_bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=InputFile(photo_file),
                        )
                    logger.info(f"✅ Фото опубликовано")
                    try:
                        os.remove(post["photo_path"])
                    except Exception:
                        pass
                except Exception as photo_err:
                    logger.warning(f"sendPhoto failed: {photo_err}, публикую без картинки")
                # Затем полный текст с футером отдельным сообщением
                await client.post(
                    f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": CHANNEL_ID,
                        "text": post["text"],
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True
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
        save_approved()
        logger.info(f"✅ Опубликовано: {slot}")
    except Exception as e:
        logger.error(f"Ошибка публикации {slot}: {e}")
