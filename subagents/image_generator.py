"""
Sub-agent: генерация картинок через DALL-E 3 (OpenAI).
"""
import os
import logging

import httpx

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PHOTOS_DIR = "/data/photos"


async def generate_image(brief: str, filename: str) -> str | None:
    """Генерирует картинку по ТЗ через DALL-E 3. Возвращает путь к файлу или None."""
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
                    "model": "dall-e-3",
                    "prompt": brief,
                    "n": 1,
                    "size": "1792x1024",
                    "quality": "standard",
                }
            )
            data = resp.json()
            if "error" in data:
                logger.error(f"DALL-E 3 API error: {data['error']}")
                return None
            image_url = data["data"][0]["url"]

            img_resp = await client.get(image_url, timeout=60)
            local_path = f"{PHOTOS_DIR}/{filename}.jpg"
            with open(local_path, "wb") as f:
                f.write(img_resp.content)

            logger.info(f"✅ Картинка сгенерирована: {local_path}")
            return local_path
    except Exception as e:
        logger.error(f"generate_image error: {e}")
        return None
