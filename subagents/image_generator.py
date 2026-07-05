"""
Sub-agent: генерация картинок через gpt-image-1 (OpenAI).
"""
import os
import base64
import logging

import httpx

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PHOTOS_DIR = "/data/photos"


async def generate_image(brief: str, filename: str) -> str | None:
    """Генерирует картинку по ТЗ через gpt-image-1. Возвращает путь к файлу или None."""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY не задан — пропускаем генерацию картинки")
        return None
    try:
        os.makedirs(PHOTOS_DIR, exist_ok=True)
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/images/generations",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-image-1",
                    "prompt": brief,
                    "n": 1,
                    "size": "1536x1024",
                    "quality": "high",
                }
            )
            data = resp.json()
            if "error" in data:
                logger.error(f"gpt-image-1 API error: {data['error']}")
                return None
            image_b64 = data["data"][0]["b64_json"]

            local_path = f"{PHOTOS_DIR}/{filename}.jpg"
            with open(local_path, "wb") as f:
                f.write(base64.b64decode(image_b64))

            logger.info(f"✅ Картинка сгенерирована: {local_path}")
            return local_path
    except Exception as e:
        logger.error(f"generate_image error: {e}")
        return None
