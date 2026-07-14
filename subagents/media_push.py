"""
Отправляет сгенерированные медиа-файлы на web (у Catapult-Bot и web разные volume
на Railway, /data не общий — см. Task 14 диагностику), чтобы web мог раздать их
JSON2Video через /media.
"""
import os
import logging

import httpx

logger = logging.getLogger(__name__)

BACKEND_URL       = os.getenv("BACKEND_URL", "https://web-production-9851f.up.railway.app")
MEDIA_SERVE_TOKEN = os.getenv("MEDIA_SERVE_TOKEN", "")


async def push_media(kind: str, local_path: str) -> bool:
    """Загружает local_path на web под тем же именем. Возвращает True при успехе."""
    filename = os.path.basename(local_path)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(local_path, "rb") as f:
                resp = await client.post(
                    f"{BACKEND_URL}/media/{kind}/{filename}",
                    params={"token": MEDIA_SERVE_TOKEN},
                    files={"file": (filename, f)},
                )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"push_media({kind}, {filename}) error: {e}")
        return False


def media_url(kind: str, local_path: str) -> str:
    filename = os.path.basename(local_path)
    return f"{BACKEND_URL}/media/{kind}/{filename}?token={MEDIA_SERVE_TOKEN}"
