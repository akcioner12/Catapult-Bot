"""
Sub-agent: публикация фото-постов и Reels в Instagram через Buffer.
"""
import os
import logging

from subagents.buffer_publisher import publish_to_buffer
from subagents.media_push import push_media, media_url
from subagents.instagram_caption import generate_instagram_caption

logger = logging.getLogger(__name__)

BUFFER_INSTAGRAM_CHANNEL_ID = os.getenv("BUFFER_INSTAGRAM_CHANNEL_ID", "")


def _full_caption(parts: dict) -> str:
    if parts["hashtags"]:
        return f"{parts['caption']}\n\n{' '.join(parts['hashtags'])}"
    return parts["caption"]


async def upload_photo_to_instagram(photo_path: str, source_text: str, category: str) -> str | None:
    """Публикует photo_path в Instagram (лента) через Buffer. Файл уже должен
    быть на web (generate_image пушит его при генерации, image_generator.py) —
    не пушится повторно здесь. Возвращает ссылку на пост или None."""
    if not BUFFER_INSTAGRAM_CHANNEL_ID:
        logger.warning("BUFFER_INSTAGRAM_CHANNEL_ID не задан — пропускаем публикацию в Instagram")
        return None
    if not os.path.exists(photo_path):
        logger.error(f"upload_photo_to_instagram: файл не найден {photo_path}")
        return None

    parts = await generate_instagram_caption(source_text, category, "photo")
    return await publish_to_buffer(
        BUFFER_INSTAGRAM_CHANNEL_ID, _full_caption(parts), media_url("photos", photo_path), "image"
    )


async def upload_reel_to_instagram(video_path: str, source_text: str, category: str) -> str | None:
    """Публикует video_path в Instagram Reels через Buffer. Возвращает ссылку
    на пост или None."""
    if not BUFFER_INSTAGRAM_CHANNEL_ID:
        logger.warning("BUFFER_INSTAGRAM_CHANNEL_ID не задан — пропускаем публикацию в Instagram")
        return None
    if not os.path.exists(video_path):
        logger.error(f"upload_reel_to_instagram: файл не найден {video_path}")
        return None
    if not await push_media("videos", video_path):
        logger.error("upload_reel_to_instagram: не удалось выложить видео на web")
        return None

    parts = await generate_instagram_caption(source_text, category, "reel")
    return await publish_to_buffer(
        BUFFER_INSTAGRAM_CHANNEL_ID, _full_caption(parts), media_url("videos", video_path), "video"
    )
