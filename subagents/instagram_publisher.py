"""
Sub-agent: публикация фото-постов и Reels в Instagram через Buffer.
"""
import os
import time
import logging

from subagents.buffer_publisher import publish_to_buffer
from subagents.media_push import push_media, media_url
from subagents.instagram_caption import generate_instagram_caption
from subagents.image_generator import generate_image

logger = logging.getLogger(__name__)

BUFFER_INSTAGRAM_CHANNEL_ID = os.getenv("BUFFER_INSTAGRAM_CHANNEL_ID", "")


def _full_caption(parts: dict) -> str:
    if parts["hashtags"]:
        return f"{parts['caption']}\n\n{' '.join(parts['hashtags'])}"
    return parts["caption"]


async def upload_photo_to_instagram(brief: str, source_text: str, category: str) -> tuple[str | None, str | None]:
    """Генерирует отдельную вертикальную (9:16) картинку по тому же ТЗ (brief),
    что и горизонтальная картинка поста для Telegram, и публикует именно её в
    Instagram (лента) через Buffer. Горизонтальная картинка для Telegram не
    переиспользуется — Instagram обрезает её вбок, а не просто ужимает, из-за
    чего половина сюжета терялась. generate_image сам пушит файл на web, так
    что отдельный push_media здесь не нужен. Возвращает (ссылка, None) при
    успехе или (None, причина) при сбое — причина для админ-уведомления."""
    if not BUFFER_INSTAGRAM_CHANNEL_ID:
        logger.warning("BUFFER_INSTAGRAM_CHANNEL_ID не задан — пропускаем публикацию в Instagram")
        return None, None

    ig_photo_path = await generate_image(brief, f"ig_{int(time.time())}", aspect_ratio="9:16")
    if not ig_photo_path:
        logger.error("upload_photo_to_instagram: не удалось сгенерировать вертикальную картинку")
        return None, "не удалось сгенерировать картинку"

    parts = await generate_instagram_caption(source_text, category, "photo")
    return await publish_to_buffer(
        BUFFER_INSTAGRAM_CHANNEL_ID, _full_caption(parts), media_url("photos", ig_photo_path), "image",
        metadata={"instagram": {"type": "post", "shouldShareToFeed": True}},
    )


async def upload_reel_to_instagram(video_path: str, source_text: str, category: str) -> tuple[str | None, str | None]:
    """Публикует video_path в Instagram Reels через Buffer. Возвращает
    (ссылка, None) при успехе или (None, причина) при сбое."""
    if not BUFFER_INSTAGRAM_CHANNEL_ID:
        logger.warning("BUFFER_INSTAGRAM_CHANNEL_ID не задан — пропускаем публикацию в Instagram")
        return None, None
    if not os.path.exists(video_path):
        logger.error(f"upload_reel_to_instagram: файл не найден {video_path}")
        return None, "файл видео не найден"
    if not await push_media("videos", video_path):
        logger.error("upload_reel_to_instagram: не удалось выложить видео на web")
        return None, "не удалось выложить видео на web"

    parts = await generate_instagram_caption(source_text, category, "reel")
    return await publish_to_buffer(
        BUFFER_INSTAGRAM_CHANNEL_ID, _full_caption(parts), media_url("videos", video_path), "video",
        metadata={"instagram": {"type": "reel", "shouldShareToFeed": True}},
    )
