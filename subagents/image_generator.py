"""
Sub-agent: генерация картинок через Pollinations.ai (бесплатно, без API-ключа).
"""
import logging
import os
import urllib.parse

import httpx

from subagents.media_push import push_media

logger = logging.getLogger(__name__)

PHOTOS_DIR = "/data/photos"
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"


async def generate_image(brief: str, filename: str) -> str | None:
    """Генерирует картинку по ТЗ через Pollinations.ai. Возвращает путь к файлу или None."""
    try:
        os.makedirs(PHOTOS_DIR, exist_ok=True)
        prompt = urllib.parse.quote(brief[:800])
        url = POLLINATIONS_URL.format(prompt=prompt)
        async with httpx.AsyncClient(timeout=60) as client:
            resp = None
            for attempt in range(3):
                resp = await client.get(
                    url,
                    params={"width": 1080, "height": 1920, "nologo": "true"},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if resp.status_code < 500:
                    break
                logger.warning(f"Pollinations {resp.status_code} на попытке {attempt + 1}/3 — повтор")
            resp.raise_for_status()

            local_path = f"{PHOTOS_DIR}/{filename}.jpg"
            with open(local_path, "wb") as f:
                f.write(resp.content)

            await push_media("photos", local_path)
            logger.info(f"✅ Картинка сгенерирована: {local_path}")
            return local_path
    except Exception as e:
        logger.error(f"generate_image error: {e}")
        return None
