"""
Sub-agent: approval-цикл для YouTube Shorts + загрузка на YouTube + анонс в TG.
Зеркалит форму tg_publisher.py, но для видео.
"""
import os
import time
import json
import secrets
import asyncio
import hashlib
import logging

import httpx
from telegram import Bot, InputFile
from telegram.ext import ContextTypes

from subagents.tiktok_publisher import upload_to_tiktok

logger = logging.getLogger(__name__)

VIDEOS_DIR = "/data/videos"
os.makedirs(VIDEOS_DIR, exist_ok=True)

PENDING_FILE  = "/data/pending_videos.json"
APPROVED_FILE = "/data/approved_videos.json"
UPLOAD_TOKENS_FILE = "/data/upload_tokens.json"
PENDING_UPLOADS_FILE = "/data/pending_uploads.json"
TIKTOK_RETRY_FILE = "/data/tiktok_retry_pending.json"

pending_videos: dict = {}
approved_videos: dict = {}
editing_video_title: dict = {}
awaiting_self_record_video: dict = {}
tiktok_retry_pending: dict = {}

PARSER_BOT_TOKEN = None
ADMIN_TG_ID = None
MAIN_BOT_TOKEN = None
CHANNEL_ID = None
YOUTUBE_CLIENT_ID = None
YOUTUBE_CLIENT_SECRET = None
YOUTUBE_REFRESH_TOKEN = None
YOUTUBE_CATEGORY_ID = "22"
YOUTUBE_PRIVACY_STATUS = "public"

def configure(parser_bot_token, admin_tg_id, main_bot_token, channel_id,
              youtube_client_id, youtube_client_secret, youtube_refresh_token,
              youtube_category_id="22", youtube_privacy_status="public"):
    global PARSER_BOT_TOKEN, ADMIN_TG_ID, MAIN_BOT_TOKEN, CHANNEL_ID
    global YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
    global YOUTUBE_CATEGORY_ID, YOUTUBE_PRIVACY_STATUS
    PARSER_BOT_TOKEN = parser_bot_token
    ADMIN_TG_ID = admin_tg_id
    MAIN_BOT_TOKEN = main_bot_token
    CHANNEL_ID = channel_id
    YOUTUBE_CLIENT_ID = youtube_client_id
    YOUTUBE_CLIENT_SECRET = youtube_client_secret
    YOUTUBE_REFRESH_TOKEN = youtube_refresh_token
    YOUTUBE_CATEGORY_ID = youtube_category_id
    YOUTUBE_PRIVACY_STATUS = youtube_privacy_status

def save_pending_videos():
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(pending_videos, f, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"Save pending videos error: {e}")

def save_approved_videos():
    try:
        with open(APPROVED_FILE, "w", encoding="utf-8") as f:
            json.dump(approved_videos, f, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"Save approved videos error: {e}")

def save_tiktok_retry_pending():
    try:
        with open(TIKTOK_RETRY_FILE, "w", encoding="utf-8") as f:
            json.dump(tiktok_retry_pending, f, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"Save tiktok retry pending error: {e}")

def load_pending_videos():
    try:
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                pending_videos.clear()
                pending_videos.update(json.load(f))
            logger.info(f"Загружено {len(pending_videos)} pending видео")
    except Exception as e:
        logger.error(f"Load pending videos error: {e}")
    try:
        if os.path.exists(APPROVED_FILE):
            with open(APPROVED_FILE, "r", encoding="utf-8") as f:
                approved_videos.update(json.load(f))
            logger.info(f"Загружено {len(approved_videos)} approved видео")
    except Exception as e:
        logger.error(f"Load approved videos error: {e}")
    try:
        if os.path.exists(TIKTOK_RETRY_FILE):
            with open(TIKTOK_RETRY_FILE, "r", encoding="utf-8") as f:
                tiktok_retry_pending.update(json.load(f))
            logger.info(f"Загружено {len(tiktok_retry_pending)} видео на повтор TikTok")
    except Exception as e:
        logger.error(f"Load tiktok retry pending error: {e}")

# ── Клавиатура одобрения видео ────────────────────────────────────────────────
def video_approval_keyboard(video_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Одобрить и опубликовать", "callback_data": f"vapprove_{video_id}"},
        ], [
            {"text": "✏️ Изменить название", "callback_data": f"vedit_{video_id}"},
            {"text": "❌ Отменить", "callback_data": f"vcancel_{video_id}"},
        ]]
    }

# ── Отправка видео на одобрение ───────────────────────────────────────────────
async def send_video_for_approval(video_path: str, title: str, description: str, tags: list, category: str, thumbnail_path: str | None = None):
    video_id = f"{category}_{hashlib.md5(title.encode()).hexdigest()[:8]}"
    pending_videos[video_id] = {
        "video_path": video_path,
        "title": title,
        "description": description,
        "tags": tags,
        "category": category,
        "thumbnail_path": thumbnail_path,
    }
    save_pending_videos()

    try:
        bot = Bot(token=PARSER_BOT_TOKEN)
        caption = (
            f"🎬 <b>Новый Short готов!</b> [{category.upper()}]\n\n"
            f"📌 {title}\n\n{description[:500]}"
        )
        if os.path.getsize(video_path) < 45 * 1024 * 1024:
            with open(video_path, "rb") as video_file:
                await bot.send_video(
                    chat_id=ADMIN_TG_ID,
                    video=InputFile(video_file),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=video_approval_keyboard(video_id),
                )
        else:
            await bot.send_message(
                chat_id=ADMIN_TG_ID,
                text=caption + "\n\n⚠️ Файл слишком большой для превью в Telegram — одобри вслепую по названию/описанию, или проверь файл на сервере вручную.",
                parse_mode="HTML",
                reply_markup=video_approval_keyboard(video_id),
            )
    except Exception as e:
        logger.error(f"send_video_for_approval error: {e}")

# ── Правка текста/caption сообщения-одобрения ──────────────────────────────
async def _edit_status(query, text: str, **kwargs):
    """query.message может быть видео с caption (send_video) или обычным текстом
    (send_message в ветке "файл слишком большой") — edit_message_text падает на видео."""
    if query.message.video or query.message.photo:
        await query.edit_message_caption(caption=text, **kwargs)
    else:
        await query.edit_message_text(text, **kwargs)

# ── Обработчик кнопок одобрения видео ─────────────────────────────────────────
async def handle_video_approval(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, video_id = query.data.split("_", 1)
    video = pending_videos.get(video_id)
    if not video:
        await _edit_status(query, "⚠️ Видео не найдено или уже обработано.")
        return

    if action == "vapprove":
        await _edit_status(query, "⏳ Загружаю на YouTube...")
        pending_videos.pop(video_id, None)
        approved_videos[video_id] = video
        save_pending_videos()
        save_approved_videos()

        youtube_id = await upload_to_youtube(video["video_path"], video["title"], video["description"], video["tags"])
        if youtube_id:
            approved_videos.pop(video_id, None)
            save_approved_videos()
            await _finish_publish(video_id, video, youtube_id)
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(
                    f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": ADMIN_TG_ID,
                        "text": "❌ Загрузка на YouTube не удалась. Видео сохранено — попробуй /retry_videos позже.",
                        "parse_mode": "HTML",
                    },
                )

    elif action == "vedit":
        editing_video_title[ADMIN_TG_ID] = video_id
        await _edit_status(query, "✏️ Пришли новое название видео. Для отмены: /cancel", parse_mode="HTML")

    elif action == "vcancel":
        pending_videos.pop(video_id, None)
        save_pending_videos()
        await _edit_status(query, "❌ Видео отменено.")

# ── Обработчик текста при редактировании названия ────────────────────────────
async def handle_video_title_edit(update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    video_id = editing_video_title.get(admin_id)
    if not video_id:
        return
    video = pending_videos.get(video_id)
    if not video:
        editing_video_title.pop(admin_id, None)
        await update.message.reply_text("⚠️ Видео не найдено.")
        return

    video["title"] = update.message.text[:100]
    editing_video_title.pop(admin_id, None)
    save_pending_videos()
    await update.message.reply_text(
        f"✅ <b>Название обновлено:</b>\n{video['title']}",
        parse_mode="HTML",
        reply_markup=video_approval_keyboard(video_id),
    )

# ── Обработчик видео, присланного админом (самозапись) ───────────────────────
async def handle_video_file(update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    state = awaiting_self_record_video.get(admin_id)
    if not state:
        return

    await update.message.reply_text("⏳ Скачиваю видео на сервер...")
    try:
        video = update.message.video
        file_id = video.file_id
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/getFile",
                params={"file_id": file_id},
            )
            file_path = resp.json()["result"]["file_path"]
            file_resp = await client.get(
                f"https://api.telegram.org/file/bot{PARSER_BOT_TOKEN}/{file_path}"
            )
            local_path = f"{VIDEOS_DIR}/self_{int(time.time())}.mp4"
            with open(local_path, "wb") as f:
                f.write(file_resp.content)

        awaiting_self_record_video.pop(admin_id, None)

        from subagents.yt_script import generate_video_metadata
        metadata = await generate_video_metadata(state["topic"], state["script"], state["category"])
        if not metadata:
            await update.message.reply_text("❌ Не удалось подготовить название/описание. Попробуй прислать видео ещё раз позже.")
            return

        await send_video_for_approval(
            local_path, metadata["title"], metadata["description"], metadata["tags"], state["category"]
        )
    except Exception as e:
        logger.error(f"handle_video_file error: {e}")
        await update.message.reply_text(f"❌ Ошибка при обработке видео: {e}")

# ── YouTube: сервис и загрузка ────────────────────────────────────────────────
def get_youtube_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    import googleapiclient.discovery

    creds = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"],
    )
    creds.refresh(Request())
    return googleapiclient.discovery.build("youtube", "v3", credentials=creds)

def _upload_sync(video_path: str, title: str, description: str, tags: list) -> str:
    from googleapiclient.http import MediaFileUpload

    youtube = get_youtube_service()
    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags,
            "categoryId": YOUTUBE_CATEGORY_ID,
            "defaultLanguage": "ru",
        },
        "status": {
            "privacyStatus": YOUTUBE_PRIVACY_STATUS,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True, chunksize=10 * 1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]

async def upload_to_youtube(video_path: str, title: str, description: str, tags: list) -> str | None:
    if not YOUTUBE_CLIENT_ID or not YOUTUBE_CLIENT_SECRET or not YOUTUBE_REFRESH_TOKEN:
        logger.warning("YouTube OAuth не настроен — пропускаем загрузку")
        return None
    if not os.path.exists(video_path):
        logger.error(f"upload_to_youtube: файл не найден {video_path}")
        return None
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _upload_sync, video_path, title, description, tags)
    except Exception as e:
        logger.error(f"upload_to_youtube error: {e}")
        return None

async def announce_in_telegram(youtube_video_id: str, title: str = "", thumbnail_path: str | None = None):
    caption = f"🎬 <b>Новый ролик на YouTube!</b>\n\n📌 {title}\n\nhttps://youtu.be/{youtube_video_id}" if title \
        else f"🎬 <b>Новый ролик на YouTube!</b>\n\nhttps://youtu.be/{youtube_video_id}"
    try:
        bot = Bot(token=MAIN_BOT_TOKEN)
        if thumbnail_path and os.path.exists(thumbnail_path):
            with open(thumbnail_path, "rb") as photo_file:
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=InputFile(photo_file),
                    caption=caption,
                    parse_mode="HTML",
                )
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=caption, parse_mode="HTML")
    except Exception as e:
        logger.error(f"announce_in_telegram error: {e}")

async def _finish_publish(video_id: str, video: dict, youtube_id: str):
    """После успешной загрузки на YouTube: анонсирует в канале, пробует TikTok,
    и шлёт админу сводку по обеим площадкам. Общий код для первого одобрения
    и для /retry_videos."""
    await announce_in_telegram(youtube_id, video["title"], video.get("thumbnail_path"))

    tiktok_url = await upload_to_tiktok(video["video_path"], video["title"])
    status_lines = [f"✅ YouTube: https://youtu.be/{youtube_id}"]
    if tiktok_url:
        status_lines.append(f"✅ TikTok: {tiktok_url}")
        try:
            os.remove(video["video_path"])
        except Exception:
            pass
    else:
        status_lines.append("⚠️ TikTok не удался — /retry_tiktok")
        tiktok_retry_pending[video_id] = video
        save_tiktok_retry_pending()

    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": "<b>Видео опубликовано:</b>\n" + "\n".join(status_lines),
                "parse_mode": "HTML",
            },
        )

async def retry_upload(video_id: str):
    video = approved_videos.get(video_id)
    if not video:
        return
    youtube_id = await upload_to_youtube(video["video_path"], video["title"], video["description"], video["tags"])
    if not youtube_id:
        return
    approved_videos.pop(video_id, None)
    save_approved_videos()
    await _finish_publish(video_id, video, youtube_id)

async def retry_tiktok_upload(video_id: str):
    video = tiktok_retry_pending.get(video_id)
    if not video:
        return
    tiktok_url = await upload_to_tiktok(video["video_path"], video["title"])
    if not tiktok_url:
        return
    tiktok_retry_pending.pop(video_id, None)
    save_tiktok_retry_pending()
    try:
        os.remove(video["video_path"])
    except Exception:
        pass
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{PARSER_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_TG_ID,
                "text": f"✅ <b>TikTok опубликован (повтор)!</b>\n{tiktok_url}",
                "parse_mode": "HTML",
            },
        )

# ── Токены загрузки для самозаписи (обходим лимит getFile в 20МБ) ───────────
def _read_json_file(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Read {path} error: {e}")
    return default

def _write_json_file(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Write {path} error: {e}")

def create_upload_token(topic: str, script: str, category: str) -> str:
    token = secrets.token_urlsafe(16)
    tokens = _read_json_file(UPLOAD_TOKENS_FILE, {})
    tokens[token] = {"topic": topic, "script": script, "category": category}
    _write_json_file(UPLOAD_TOKENS_FILE, tokens)
    return token

def get_upload_token_info(token: str) -> dict | None:
    tokens = _read_json_file(UPLOAD_TOKENS_FILE, {})
    return tokens.get(token)

def consume_upload_token(token: str, video_path: str) -> bool:
    tokens = _read_json_file(UPLOAD_TOKENS_FILE, {})
    info = tokens.pop(token, None)
    if not info:
        return False
    _write_json_file(UPLOAD_TOKENS_FILE, tokens)

    uploads = _read_json_file(PENDING_UPLOADS_FILE, [])
    uploads.append({
        "video_path": video_path,
        "topic": info["topic"],
        "script": info["script"],
        "category": info["category"],
    })
    _write_json_file(PENDING_UPLOADS_FILE, uploads)
    return True

def pop_pending_uploads() -> list:
    uploads = _read_json_file(PENDING_UPLOADS_FILE, [])
    _write_json_file(PENDING_UPLOADS_FILE, [])
    return uploads
