"""
Sub-agent: публикация видео в TikTok через Buffer (уже прошедший аудит
TikTok Content Posting API — без него загрузка через официальный API TikTok
была бы видна только самому аккаунту, не подписчикам).
"""
import asyncio
import logging
import os

import httpx

from subagents.media_push import push_media, BACKEND_URL, MEDIA_SERVE_TOKEN

logger = logging.getLogger(__name__)

BUFFER_API_KEY = os.getenv("BUFFER_API_KEY", "")
BUFFER_TIKTOK_CHANNEL_ID = os.getenv("BUFFER_TIKTOK_CHANNEL_ID", "")
BUFFER_URL = "https://api.buffer.com"

CREATE_POST_MUTATION = """
mutation($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id status } }
    ... on InvalidInputError { message }
    ... on UnauthorizedError { message }
    ... on UnexpectedError { message }
    ... on LimitReachedError { message }
    ... on NotFoundError { message }
    ... on RestProxyError { message }
  }
}
"""

POST_STATUS_QUERY = """
query($id: PostId!) {
  post(input: {id: $id}) { id status sentAt externalLink }
}
"""


def _media_url(kind: str, local_path: str) -> str:
    filename = os.path.basename(local_path)
    return f"{BACKEND_URL}/media/{kind}/{filename}?token={MEDIA_SERVE_TOKEN}"


async def upload_to_tiktok(video_path: str, caption: str) -> str | None:
    """Публикует video_path в TikTok через Buffer. Возвращает ссылку на пост или None."""
    if not BUFFER_API_KEY or not BUFFER_TIKTOK_CHANNEL_ID:
        logger.warning("BUFFER_API_KEY/BUFFER_TIKTOK_CHANNEL_ID не заданы — пропускаем публикацию в TikTok")
        return None
    if not os.path.exists(video_path):
        logger.error(f"upload_to_tiktok: файл не найден {video_path}")
        return None
    if not await push_media("videos", video_path):
        logger.error("upload_to_tiktok: не удалось выложить видео на web")
        return None

    video_url = _media_url("videos", video_path)
    headers = {"Authorization": f"Bearer {BUFFER_API_KEY}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                BUFFER_URL,
                headers=headers,
                json={
                    "query": CREATE_POST_MUTATION,
                    "variables": {
                        "input": {
                            "channelId": BUFFER_TIKTOK_CHANNEL_ID,
                            "text": caption[:2200],
                            "mode": "shareNow",
                            "schedulingType": "automatic",
                            "assets": [{"video": {"url": video_url}}],
                        }
                    },
                },
            )
            data = resp.json()
            result = data.get("data", {}).get("createPost", {})
            if "message" in result:
                logger.error(f"Buffer createPost error: {result['message']}")
                return None
            post_id = result.get("post", {}).get("id")
            if not post_id:
                logger.error(f"Buffer createPost: неожиданный ответ: {data}")
                return None

            for _ in range(24):  # до ~2 минут ожидания публикации
                await asyncio.sleep(5)
                status_resp = await client.post(
                    BUFFER_URL,
                    headers=headers,
                    json={"query": POST_STATUS_QUERY, "variables": {"id": post_id}},
                )
                post = status_resp.json().get("data", {}).get("post", {})
                if post.get("status") == "sent":
                    logger.info(f"✅ Опубликовано в TikTok: {post['externalLink']}")
                    return post["externalLink"]
                if post.get("status") == "error":
                    logger.error(f"Buffer: публикация в TikTok завершилась ошибкой: {post}")
                    return None

            logger.error("Buffer: публикация в TikTok не завершилась за отведённое время")
            return None
    except Exception as e:
        logger.error(f"upload_to_tiktok error: {e}")
        return None
