"""
Sub-agent: сборка вертикального видео (картинки + озвучка + авто-субтитры) через JSON2Video.
JSON2Video принимает только публичные HTTPS-ссылки на ассеты — локальные файлы
раздаются через /media эндпоинт в server.py (см. Task 2 плана).
"""
import os
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

JSON2VIDEO_API_KEY = os.getenv("JSON2VIDEO_API_KEY", "")
BACKEND_URL         = os.getenv("BACKEND_URL", "https://web-production-9851f.up.railway.app")
MEDIA_SERVE_TOKEN   = os.getenv("MEDIA_SERVE_TOKEN", "")
VIDEOS_DIR = "/data/videos"
os.makedirs(VIDEOS_DIR, exist_ok=True)

WORDS_PER_SECOND = 2.5  # оценка длительности озвучки по числу слов — mp3 не декодируем

def _media_url(kind: str, local_path: str) -> str:
    filename = os.path.basename(local_path)
    return f"{BACKEND_URL}/media/{kind}/{filename}?token={MEDIA_SERVE_TOKEN}"

async def render_video(script_text: str, image_paths: list[str], audio_path: str, filename: str) -> str | None:
    """Рендерит вертикальное видео 1080x1920 через JSON2Video. None при сбое/нехватке кредитов/таймауте."""
    if not JSON2VIDEO_API_KEY:
        logger.warning("JSON2VIDEO_API_KEY не задан — пропускаем рендер видео")
        return None
    if not image_paths or not audio_path:
        logger.warning("render_video: нет картинок или озвучки — пропускаем")
        return None

    total_seconds = max(len(script_text.split()) / WORDS_PER_SECOND, len(image_paths) * 3)
    duration_per_image = total_seconds / len(image_paths)

    movie = {
        "resolution": "custom",
        "width": 1080,
        "height": 1920,
        "scenes": [
            {
                "elements": [{
                    "type": "image",
                    "src": _media_url("photos", path),
                    "duration": duration_per_image,
                    "position": "center-center",
                    "resize": "cover",
                    "zoom": 2,
                    "pan": "right" if i % 2 == 0 else "left",
                }]
            }
            for i, path in enumerate(image_paths)
        ],
        "elements": [
            {"type": "audio", "src": _media_url("audio", audio_path), "duration": -1},
            {"type": "subtitles"},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.json2video.com/v2/movies",
                headers={"x-api-key": JSON2VIDEO_API_KEY, "Content-Type": "application/json"},
                json=movie,
            )
            if resp.status_code in (402, 429):
                logger.warning(f"JSON2Video: нет кредитов/лимит ({resp.status_code}) — пропускаем рендер")
                return None
            data = resp.json()
            project_id = data.get("project")
            if not project_id:
                logger.error(f"JSON2Video: не удалось создать проект: {data}")
                return None

            for _ in range(60):  # до ~10 минут ожидания рендера
                await asyncio.sleep(10)
                status_resp = await client.get(
                    "https://api.json2video.com/v2/movies",
                    headers={"x-api-key": JSON2VIDEO_API_KEY},
                    params={"project": project_id},
                )
                movie_status = status_resp.json().get("movie", {})
                status = movie_status.get("status")
                if status == "done":
                    video_resp = await client.get(movie_status["url"])
                    local_path = f"{VIDEOS_DIR}/{filename}.mp4"
                    with open(local_path, "wb") as f:
                        f.write(video_resp.content)
                    logger.info(f"✅ Видео отрендерено: {local_path}")
                    return local_path
                if status in ("error", "timeout"):
                    logger.error(f"JSON2Video render failed: {movie_status.get('message')}")
                    return None

            logger.error("JSON2Video: рендер не завершился за отведённое время")
            return None
    except Exception as e:
        logger.error(f"render_video error: {e}")
        return None
