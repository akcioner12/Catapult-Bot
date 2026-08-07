"""
Sub-agent: публикация видео в личные Stories пользователя (не канала) через
MTProto (Telethon). Обычный Bot API этого не умеет (postStory работает только
для Business-аккаунтов) — нужен юзер-аккаунт, тот же, что залогинен сессией.
Личные сторис не требуют буста канала (это ограничение только для каналов/
супергрупп) — работают сразу для любого аккаунта.
"""
import asyncio
import logging
import os

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl import types, functions

logger = logging.getLogger(__name__)

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or 0)
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "")

STORY_PERIOD_SECONDS = 24 * 3600  # висит 24ч перед архивом — макс. охват без ручного продления

# Telegram недокументированно отклоняет Stories с высоким битрейтом
# (MEDIA_FILE_INVALID) — обнаружено эмпирически: видео на ~2.5 Мбит/с падало,
# на ~1.3 Мбит/с проходило. Порог с запасом ниже наблюдавшегося рабочего уровня.
MAX_VIDEO_BITRATE = 1_600_000

# Позиция кликабельного стикера-ссылки — узкая полоса ближе к низу кадра,
# координаты/размеры в процентах от кадра (x/y — центр области).
LINK_AREA_COORDINATES = types.MediaAreaCoordinates(x=50.0, y=90.0, w=90.0, h=8.0, rotation=0.0)


async def _ensure_safe_bitrate(video_path: str) -> str:
    """Если битрейт видеодорожки выше MAX_VIDEO_BITRATE — перекодирует в отдельный
    временный файл с ограниченным битрейтом. Иначе возвращает video_path без
    изменений. При сбое перекодирования — тоже возвращает video_path (пусть
    Telegram сам решит, не хуже, чем без перекодирования)."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=bit_rate", "-of", "csv=p=0", video_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        bitrate = int(stdout.decode().strip())
    except ValueError:
        return video_path
    if bitrate <= MAX_VIDEO_BITRATE:
        return video_path

    safe_path = f"{video_path.rsplit('.', 1)[0]}_storysafe.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", video_path,
        "-c:v", "libx264", "-b:v", str(MAX_VIDEO_BITRATE),
        "-maxrate", str(MAX_VIDEO_BITRATE), "-bufsize", str(MAX_VIDEO_BITRATE * 2),
        "-c:a", "aac", "-b:a", "96k",
        safe_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error(f"_ensure_safe_bitrate: ffmpeg re-encode failed: {stderr.decode()[-500:]}")
        return video_path
    logger.info(f"Stories: перекодировано под безопасный битрейт ({bitrate} -> {MAX_VIDEO_BITRATE})")
    return safe_path


async def post_story(video_path: str, caption: str, link_url: str) -> tuple[bool, str | None]:
    """Публикует video_path в личные Stories аккаунта (не канала) с кликабельным
    стикером на link_url и текстовой подписью caption. Возвращает (успех, причина_ошибки).
    Никогда не бросает исключение."""
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH or not TELEGRAM_SESSION_STRING:
        logger.warning("TELEGRAM_API_ID/HASH/SESSION_STRING не заданы — пропускаем публикацию Stories")
        return False, None
    if not os.path.exists(video_path):
        logger.error(f"post_story: файл не найден {video_path}")
        return False, "видео-файл не найден"

    upload_path = await _ensure_safe_bitrate(video_path)
    client = TelegramClient(StringSession(TELEGRAM_SESSION_STRING), TELEGRAM_API_ID, TELEGRAM_API_HASH)
    try:
        await client.connect()
        peer = types.InputPeerSelf()
        _, media, _ = await client._file_to_media(upload_path, supports_streaming=True)

        await client(functions.stories.SendStoryRequest(
            peer=peer,
            media=media,
            privacy_rules=[types.InputPrivacyValueAllowAll()],
            media_areas=[types.MediaAreaUrl(coordinates=LINK_AREA_COORDINATES, url=link_url)],
            caption=caption[:2000],
            period=STORY_PERIOD_SECONDS,
        ))
        logger.info("✅ Stories опубликовано")
        return True, None
    except Exception as e:
        message = str(e)
        logger.error(f"post_story error: {message}")
        return False, message
    finally:
        await client.disconnect()
        if upload_path != video_path:
            try:
                os.remove(upload_path)
            except Exception:
                pass
