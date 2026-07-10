"""
Sub-agent: генерация картинок через Gemini 3.1 Flash Image (Nano Banana 2).
"""
import base64
import logging
import os

import httpx

from subagents.media_push import push_media

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent"
PHOTOS_DIR = "/data/photos"


async def generate_image(brief: str, filename: str) -> str | None:
    """Генерирует картинку по ТЗ через Gemini (Nano Banana 2). Возвращает путь к файлу или None."""
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY не задан — пропускаем генерацию картинки")
        return None
    try:
        os.makedirs(PHOTOS_DIR, exist_ok=True)
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                json={
                    "contents": [{"parts": [{"text": brief[:800]}]}],
                    "generationConfig": {"imageConfig": {"aspectRatio": "9:16"}},
                },
            )
            if resp.status_code != 200:
                logger.error(f"Gemini image API error {resp.status_code}: {resp.text[:300]}")
                return None
            data = resp.json()
            image_part = next(
                (p for p in data["candidates"][0]["content"]["parts"] if "inlineData" in p),
                None,
            )
            if not image_part:
                logger.error(f"Gemini image API: в ответе нет картинки: {data}")
                return None

            ext = "png" if "png" in image_part["inlineData"]["mimeType"] else "jpg"
            local_path = f"{PHOTOS_DIR}/{filename}.{ext}"
            with open(local_path, "wb") as f:
                f.write(base64.b64decode(image_part["inlineData"]["data"]))

            await push_media("photos", local_path)
            logger.info(f"✅ Картинка сгенерирована: {local_path}")
            return local_path
    except Exception as e:
        logger.error(f"generate_image error: {e}")
        return None
