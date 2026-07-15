"""
Sub-agent: approval-цикл и публикация в Telegram-канал.
Перенесено из parser.py. handle_admin_edit is new — it's the admin-only
body that used to be inlined inside parser.py's handle_edit_message
dispatcher; the dispatcher itself (and its non-admin branch) stays in
parser.py. No logic changed, only relocated.
"""
import os
import re
import html
import json
import random
import logging
import hashlib
from datetime import datetime

import httpx
from telegram import Update, InputFile
from telegram.ext import ContextTypes

from subagents.image_brief import generate_image_brief
from subagents.rewriter import generate_catapult_post, generate_post_claude, CATAPULT_ANGLES
from subagents.instagram_publisher import upload_photo_to_instagram

logger = logging.getLogger(__name__)

# ── Безопасное превью с обрезкой текста ───────────────────────────────────────
def preview_text(text: str, length: int) -> str:
    """Обрезка для превью с parse_mode=HTML — снимает теги, чтобы срез не порвал тег пополам."""
    return re.sub(r"<[^>]+>", "", text)[:length]

# ── Подпись канала ────────────────────────────────────────────────────────────
CHANNEL_SIGNATURE = """

———
🔔 Подпишись и не пропусти важное

▶️ <a href="https://www.youtube.com/channel/UC9C6LiSOS6y2LhTfP15XpNg">YouTube</a> | 💬 <a href="https://t.me/Crypto_AI_Forex_Chat">TG Chat</a> | 🎵 <a href="https://www.tiktok.com/@crypto_ai_forex">TikTok</a> | 📷 <a href="https://www.instagram.com/crypto.ai.forex/">Instagram</a> | 🤖 <a href="https://t.me/catapulttrade_guide_bot">TG Bot</a> | 🐦 <a href="https://x.com/cryptoaiforex">Twitter</a>"""

# ── Состояние ─────────────────────────────────────────────────────────────────
pending_posts: dict = {}
approved_queue: dict = {}
awaiting_photo: dict = {}
awaiting_photo_edit: dict = {}  # admin_id -> slot, замена картинки у поста уже в очереди

PENDING_FILE  = "/data/pending_posts.json"
APPROVED_FILE = "/data/approved_queue.json"
PHOTOS_DIR    = "/data/photos"
os.makedirs(PHOTOS_DIR, exist_ok=True)

# ── Дневной лимит на Catapult и горячие новости (не более 1 в день на категорию) ──
DAILY_STATE_FILE = "/data/daily_content_state.json"
daily_state: dict = {"date": "", "breaking_done": {}, "catapult_done": False}

def _reset_daily_state_if_new_day():
    today = datetime.utcnow().date().isoformat()
    if daily_state.get("date") != today:
        daily_state["date"] = today
        daily_state["breaking_done"] = {}
        daily_state["catapult_done"] = False
        save_daily_state()

def load_daily_state():
    try:
        if os.path.exists(DAILY_STATE_FILE):
            with open(DAILY_STATE_FILE, "r", encoding="utf-8") as f:
                daily_state.update(json.load(f))
    except Exception as e:
        logger.error(f"Load daily state error: {e}")
    _reset_daily_state_if_new_day()

def save_daily_state():
    try:
        with open(DAILY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(daily_state, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Save daily state error: {e}")

def breaking_already_posted_today(category: str) -> bool:
    _reset_daily_state_if_new_day()
    return daily_state["breaking_done"].get(category, False)

def catapult_already_posted_today() -> bool:
    _reset_daily_state_if_new_day()
    return daily_state["catapult_done"]

def mark_breaking_posted(category: str):
    _reset_daily_state_if_new_day()
    daily_state["breaking_done"][category] = True
    if category == "catapult":
        daily_state["catapult_done"] = True
    save_daily_state()

def mark_catapult_posted():
    _reset_daily_state_if_new_day()
    daily_state["catapult_done"] = True
    save_daily_state()

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
    try:
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                pending_posts.clear()
                pending_posts.update(json.load(f))
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

# ── Клавиатура для уведомлений об авто-одобренных постах (уже в очереди) ─────
def queue_action_keyboard(slot: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "👁 Просмотреть полностью", "callback_data": f"qpreview_{slot}"},
            {"text": "❌ Отменить",               "callback_data": f"qcancel_{slot}"},
        ]]
    }

# ── Отправка поста на одобрение ───────────────────────────────────────────────
async def send_for_approval(post_text: str, category: str, slot: str, source: str = "", original: str = "", breaking: bool = False):
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
        "breaking": breaking,
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

    if breaking:
        preview = (
            f"🔥 <b>ГОРЯЧАЯ НОВОСТЬ — {label}</b>\n"
            f"⚡️ Одобри — опубликуется прямо сейчас\n"
            f"{'─' * 28}\n"
            f"{post_text}\n"
            f"{'─' * 28}\n"
            f"🖼 <b>ТЗ для картинки:</b>\n{brief}"
        )
    else:
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
        if post.get("breaking"):
            await query.edit_message_text("⚡️ Публикую прямо сейчас...", parse_mode="HTML")
            pending_posts.pop(post_id, None)
            save_pending()
            await publish_now(post)
        else:
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
                            f"{preview_text(new_text, 600)}\n"
                            f"{'─' * 28}\n"
                            f"🖼 <b>ТЗ для картинки:</b>\n{new_brief}"
                        ),
                        "parse_mode": "HTML",
                        "reply_markup": approval_keyboard(post_id)
                    }
                )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")

# ── Обработчик кнопок под уведомлением об авто-одобренном посте ──────────────
async def handle_queue_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, slot = query.data.split("_", 1)
    post = approved_queue.get(slot)

    if not post:
        await query.edit_message_text("⚠️ Пост не найден в очереди (уже опубликован или отменён).")
        return

    if action == "qpreview":
        photo_path = post.get("photo_path")
        if photo_path and os.path.exists(photo_path):
            from telegram import Bot, InputFile
            preview_bot = Bot(token=PARSER_BOT_TOKEN)
            with open(photo_path, "rb") as photo_file:
                await preview_bot.send_photo(chat_id=ADMIN_TG_ID, photo=InputFile(photo_file))
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": ADMIN_TG_ID,
                    "text": post["text"],
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": {
                        "inline_keyboard": [
                            [{"text": "🖼 Картинка", "callback_data": f"qeditphoto_{slot}"}]
                        ] if post.get("category") == "poll" else [[
                            {"text": "✏️ Текст",    "callback_data": f"qedit_{slot}"},
                            {"text": "🖼 Картинка", "callback_data": f"qeditphoto_{slot}"},
                        ]]
                    },
                }
            )

    elif action == "qedit":
        editing_post[ADMIN_TG_ID] = f"queue:{slot}"
        await query.edit_message_text(
            "✏️ <b>Режим редактирования</b>\nПришли исправленный текст. Для отмены: /cancel",
            parse_mode="HTML"
        )
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

    elif action == "qeditphoto":
        awaiting_photo_edit[ADMIN_TG_ID] = slot
        await query.edit_message_text(
            "🖼 <b>Пришли новую картинку для этого поста.</b>\nДля отмены: /cancel",
            parse_mode="HTML"
        )
        brief = post.get("brief") or "(ТЗ не найдено для этого поста)"
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": ADMIN_TG_ID,
                    "text": (
                        "📋 <b>ТЗ для этой картинки</b> (скопируй и вставь в Adobe/Midjourney/etc):\n\n"
                        f"<code>{html.escape(brief)}</code>"
                    ),
                    "parse_mode": "HTML",
                }
            )

    elif action == "qcancel":
        approved_queue.pop(slot, None)
        save_approved()
        await query.edit_message_text("❌ Пост удалён из очереди.")

# ── Обработчик фото (только админ) ────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_TG_ID:
        return

    # Замена картинки у поста, уже стоящего в очереди
    edit_slot = awaiting_photo_edit.get(user_id)
    if edit_slot:
        post = approved_queue.get(edit_slot)
        if not post:
            awaiting_photo_edit.pop(user_id, None)
            await update.message.reply_text("⚠️ Пост не найден в очереди.")
            return

        await update.message.reply_text("⏳ Скачиваю картинку на сервер...")
        try:
            photo = update.message.photo[-1]
            file_id = photo.file_id
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/getFile",
                    params={"file_id": file_id}
                )
                file_path = resp.json()["result"]["file_path"]
                file_resp = await client.get(
                    f"https://api.telegram.org/file/bot{PARSER_BOT_TOKEN}/{file_path}"
                )
                local_path = f"{PHOTOS_DIR}/{edit_slot}.jpg"
                with open(local_path, "wb") as f:
                    f.write(file_resp.content)

            approved_queue[edit_slot]["photo_path"] = local_path
            save_approved()
            awaiting_photo_edit.pop(user_id, None)
            await update.message.reply_text(
                "✅ <b>Картинка обновлена!</b>",
                parse_mode="HTML",
                reply_markup=queue_action_keyboard(edit_slot)
            )
        except Exception as e:
            logger.error(f"Photo edit error: {e}")
            await update.message.reply_text(f"❌ Ошибка при сохранении картинки: {e}")
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
        saved_post = pending_posts[post_id]
        pending_posts.pop(post_id, None)
        save_pending()

        if saved_post.get("breaking"):
            await update.message.reply_text("⚡️ Публикую прямо сейчас...", parse_mode="HTML")
            await publish_now(saved_post)
        else:
            approved_queue[saved_post["slot"]] = saved_post
            save_approved()
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
        awaiting_photo_edit.pop(user_id, None)
        await update.message.reply_text("✅ Отменено.")
        return

    target = editing_post.get(user_id)
    if not target:
        return

    new_text = update.message.text

    if target.startswith("queue:"):
        slot = target[len("queue:"):]
        post = approved_queue.get(slot)
        if not post:
            await update.message.reply_text("⚠️ Пост не найден в очереди.")
            editing_post.pop(user_id, None)
            return

        approved_queue[slot]["text"] = new_text + CHANNEL_SIGNATURE
        editing_post.pop(user_id, None)
        save_approved()

        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": ADMIN_TG_ID,
                    "text": f"✅ <b>Текст обновлён!</b>\n\n{preview_text(new_text, 600)}",
                    "parse_mode": "HTML",
                    "reply_markup": queue_action_keyboard(slot)
                }
            )
        return

    post_id = target
    post = pending_posts.get(post_id)
    if not post:
        await update.message.reply_text("⚠️ Пост не найден.")
        editing_post.pop(user_id, None)
        return

    pending_posts[post_id]["text"] = new_text + CHANNEL_SIGNATURE
    editing_post.pop(user_id, None)

    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": (
                    f"✅ <b>Текст обновлён!</b>\n\n"
                    f"{preview_text(new_text, 600)}\n\n"
                    f"🖼 <b>ТЗ для картинки:</b>\n{post['brief']}"
                ),
                "parse_mode": "HTML",
                "reply_markup": approval_keyboard(post_id)
            }
        )

# ── Авто-одобрение поста (без участия человека) ──────────────────────────────
async def auto_approve_post(post_text: str, category: str, slot: str, brief: str, photo_path: str = None, source: str = ""):
    post_id = f"{slot}_{hashlib.md5(post_text[:50].encode()).hexdigest()[:8]}"
    post = {
        "text": post_text + CHANNEL_SIGNATURE,
        "original": post_text,
        "category": category,
        "slot": slot,
        "source": source,
        "brief": brief,
    }
    if photo_path:
        post["photo_path"] = photo_path

    approved_queue[slot] = post
    save_approved()

    emoji_map = {"crypto": "🪙 КРИПТА", "ai": "🤖 ИИ", "forex": "💹 ФОРЕКС", "catapult": "💰 CATAPULT TRADE", "poll": "📊 ОПРОС"}
    time_map = {"crypto_1": "09:00", "catapult_1": "11:00", "ai": "13:00", "catapult_2": "15:00", "poll": "16:30", "forex": "18:00", "crypto_2": "20:00"}
    label = emoji_map.get(category, category.upper())
    pub_time = time_map.get(slot, "??:??")
    photo_status = "🖼 С картинкой" if photo_path else "📝 Без картинки"

    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": (
                    f"🤖 <b>Авто-одобрено:</b> {label}\n"
                    f"📅 Публикация завтра в {pub_time} | {photo_status}\n"
                    f"{'─' * 28}\n"
                    f"{preview_text(post_text, 300)}..."
                ),
                "parse_mode": "HTML",
                "reply_markup": queue_action_keyboard(slot),
            }
        )

# ── Мгновенная публикация горячей новости (без участия человека) ─────────────
async def publish_now_auto(post_text: str, category: str, slot: str, brief: str, photo_path: str = None, source: str = ""):
    from subagents.tg_publisher import CHANNEL_SIGNATURE
    post = {
        "text": post_text + CHANNEL_SIGNATURE,
        "category": category,
        "slot": slot,
        "source": source,
        "brief": brief,
    }
    if photo_path:
        post["photo_path"] = photo_path
    await publish_now(post)

# ── Немедленная публикация горячего поста ────────────────────────────────────
async def publish_now(post: dict):
    category = post["category"]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if post.get("photo_path") and os.path.exists(post["photo_path"]):
                from telegram import Bot, InputFile
                main_bot = Bot(token=MAIN_BOT_TOKEN)
                try:
                    with open(post["photo_path"], "rb") as photo_file:
                        await main_bot.send_photo(chat_id=CHANNEL_ID, photo=InputFile(photo_file))
                    instagram_url = await upload_photo_to_instagram(post["brief"], post["text"], category)
                    if instagram_url:
                        logger.info(f"✅ Опубликовано в Instagram: {instagram_url}")
                    else:
                        await client.post(
                            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                            json={
                                "chat_id": ADMIN_TG_ID,
                                "text": "⚠️ Не удалось опубликовать фото-пост в Instagram.",
                                "parse_mode": "HTML",
                            },
                        )
                    try:
                        os.remove(post["photo_path"])
                    except Exception:
                        pass
                except Exception as photo_err:
                    logger.warning(f"publish_now sendPhoto failed: {photo_err}")
                await client.post(
                    f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage",
                    json={"chat_id": CHANNEL_ID, "text": post["text"], "parse_mode": "HTML", "disable_web_page_preview": True}
                )
            else:
                await client.post(
                    f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage",
                    json={"chat_id": CHANNEL_ID, "text": post["text"], "parse_mode": "HTML", "disable_web_page_preview": True}
                )
            await client.post(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                json={"chat_id": ADMIN_TG_ID, "text": f"✅ <b>Горячий пост опубликован!</b> [{category.upper()}]", "parse_mode": "HTML"}
            )
        logger.info(f"✅ publish_now: {category}")
    except Exception as e:
        logger.error(f"publish_now error: {e}")

# ── Автопубликация по расписанию ──────────────────────────────────────────────
async def auto_publish(slot: str):
    post = approved_queue.get(slot)
    if not post:
        logger.warning(f"Нет одобренного поста для слота {slot}")
        return

    logger.info(f"Публикую слот: {slot}")
    category = post["category"]

    if category == "catapult" and catapult_already_posted_today():
        logger.info(f"Пропускаю {slot} — по Catapult сегодня уже была публикация (дневной лимит 1 пост)")
        approved_queue.pop(slot, None)
        save_approved()
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": ADMIN_TG_ID,
                    "text": "⏭ <b>Плановый пост Catapult пропущен</b> — сегодня уже была публикация по Catapult (лимит 1 пост в день).",
                    "parse_mode": "HTML",
                },
            )
        return

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if category == "poll" and "poll_data" in post:
                poll_data = post["poll_data"]
                if post.get("photo_path") and os.path.exists(post["photo_path"]):
                    from telegram import Bot, InputFile
                    main_bot = Bot(token=MAIN_BOT_TOKEN)
                    try:
                        with open(post["photo_path"], "rb") as photo_file:
                            await main_bot.send_photo(chat_id=CHANNEL_ID, photo=InputFile(photo_file))
                        instagram_url = await upload_photo_to_instagram(post["brief"], post["text"], category)
                        if instagram_url:
                            logger.info(f"✅ Опубликовано в Instagram: {instagram_url}")
                        else:
                            await client.post(
                                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                                json={
                                    "chat_id": ADMIN_TG_ID,
                                    "text": "⚠️ Не удалось опубликовать фото-пост в Instagram.",
                                    "parse_mode": "HTML",
                                },
                            )
                        try:
                            os.remove(post["photo_path"])
                        except Exception:
                            pass
                    except Exception as photo_err:
                        logger.warning(f"poll sendPhoto failed: {photo_err}")
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
                    instagram_url = await upload_photo_to_instagram(post["brief"], post["text"], category)
                    if instagram_url:
                        logger.info(f"✅ Опубликовано в Instagram: {instagram_url}")
                    else:
                        await client.post(
                            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                            json={
                                "chat_id": ADMIN_TG_ID,
                                "text": "⚠️ Не удалось опубликовать фото-пост в Instagram.",
                                "parse_mode": "HTML",
                            },
                        )
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
        if category == "catapult":
            mark_catapult_posted()
        logger.info(f"✅ Опубликовано: {slot}")
    except Exception as e:
        logger.error(f"Ошибка публикации {slot}: {e}")
