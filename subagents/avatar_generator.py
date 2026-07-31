"""
Sub-agent: генерация видео говорящей головы (липсинк на готовую озвучку) через
Pruna AI p-video-avatar. Модель принимает фото лица + готовый аудио-файл (URL) и
не имеет своего TTS — текст туда не передаётся.
"""
import asyncio
import logging
import os

import httpx

from subagents.media_push import push_media, media_url

logger = logging.getLogger(__name__)

PRUNA_API_KEY  = os.getenv("PRUNA_API_KEY", "")
AVATAR_FACE_URL = os.getenv("AVATAR_FACE_URL", "")
VIDEOS_DIR = "/data/videos"
os.makedirs(VIDEOS_DIR, exist_ok=True)

POLL_INTERVAL_SECONDS = 10
POLL_MAX_ATTEMPTS = 24  # 24 * 10с = 240с потолок ожидания


async def generate_avatar_video(audio_path: str, filename: str) -> str | None:
    """Генерирует mp4 говорящей головы по audio_path. Возвращает путь к файлу или None."""
    if not PRUNA_API_KEY or not AVATAR_FACE_URL:
        logger.warning("PRUNA_API_KEY/AVATAR_FACE_URL не заданы — пропускаем генерацию аватара")
        return None
    try:
        if not await push_media("audio", audio_path):
            logger.error("generate_avatar_video: не удалось захостить озвучку для Pruna")
            return None
        audio_url = media_url("audio", audio_path)

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.pruna.ai/v1/predictions",
                headers={
                    "apikey": PRUNA_API_KEY,
                    "Model": "p-video-avatar",
                    "Content-Type": "application/json",
                },
                json={"input": {"image": AVATAR_FACE_URL, "audio": audio_url}},
            )
            if resp.status_code != 201:
                logger.error(f"Pruna avatar API error {resp.status_code}: {resp.text[:300]}")
                return None
            status_url = resp.json()["get_url"]

            for _ in range(POLL_MAX_ATTEMPTS):
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                status_resp = await client.get(status_url, headers={"apikey": PRUNA_API_KEY})
                status_data = status_resp.json()
                status = status_data.get("status")
                if status == "succeeded":
                    video_resp = await client.get(status_data["generation_url"], headers={"apikey": PRUNA_API_KEY})
                    local_path = f"{VIDEOS_DIR}/{filename}.mp4"
                    with open(local_path, "wb") as f:
                        f.write(video_resp.content)
                    logger.info(f"✅ Аватар-видео сгенерировано: {local_path}")
                    return local_path
                if status == "failed":
                    logger.error(f"Pruna avatar generation failed: {status_data}")
                    return None

            logger.error("generate_avatar_video: таймаут ожидания результата от Pruna")
            return None
    except Exception as e:
        logger.error(f"generate_avatar_video error: {e}")
        return None
