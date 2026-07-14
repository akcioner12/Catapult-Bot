"""
Sub-agent: публикация видео в TikTok через Buffer (уже прошедший аудит
TikTok Content Posting API — без него загрузка через официальный API TikTok
была бы видна только самому аккаунту, не подписчикам).
"""
import os
import logging

from subagents.buffer_publisher import publish_to_buffer
from subagents.media_push import push_media, media_url

logger = logging.getLogger(__name__)

BUFFER_TIKTOK_CHANNEL_ID = os.getenv("BUFFER_TIKTOK_CHANNEL_ID", "")


async def upload_to_tiktok(video_path: str, caption: str) -> str | None:
    """Публикует video_path в TikTok через Buffer. Возвращает ссылку на пост или None."""
    if not BUFFER_TIKTOK_CHANNEL_ID:
        logger.warning("BUFFER_TIKTOK_CHANNEL_ID не задан — пропускаем публикацию в TikTok")
        return None
    if not os.path.exists(video_path):
        logger.error(f"upload_to_tiktok: файл не найден {video_path}")
        return None
    if not await push_media("videos", video_path):
        logger.error("upload_to_tiktok: не удалось выложить видео на web")
        return None

    return await publish_to_buffer(BUFFER_TIKTOK_CHANNEL_ID, caption, media_url("videos", video_path), "video")
