"""
Sub-agent: публикация видео в личные Stories пользователя (не канала) через
MTProto (Telethon). Обычный Bot API этого не умеет (postStory работает только
для Business-аккаунтов) — нужен юзер-аккаунт, тот же, что залогинен сессией.
Личные сторис не требуют буста канала (это ограничение только для каналов/
супергрупп) — работают сразу для любого аккаунта.
"""
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

# Позиция кликабельного стикера-ссылки — узкая полоса ближе к низу кадра,
# координаты/размеры в процентах от кадра (x/y — центр области).
LINK_AREA_COORDINATES = types.MediaAreaCoordinates(x=50.0, y=90.0, w=90.0, h=8.0, rotation=0.0)


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

    client = TelegramClient(StringSession(TELEGRAM_SESSION_STRING), TELEGRAM_API_ID, TELEGRAM_API_HASH)
    try:
        await client.connect()
        peer = types.InputPeerSelf()
        _, media, _ = await client._file_to_media(video_path, supports_streaming=True)

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
