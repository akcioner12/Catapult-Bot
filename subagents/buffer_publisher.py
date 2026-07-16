"""
Sub-agent: общая логика публикации через Buffer (используется и TikTok, и
Instagram паблишерами) — GraphQL createPost + поллинг статуса до sent/error.
"""
import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

BUFFER_API_KEY = os.getenv("BUFFER_API_KEY", "")
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
  post(input: {id: $id}) { id status sentAt externalLink error { message } }
}
"""


async def publish_to_buffer(
    channel_id: str, caption: str, media_url: str, media_type: str, metadata: dict | None = None
) -> tuple[str | None, str | None]:
    """Публикует media_url (уже публичный HTTPS-адрес) в канал channel_id через
    Buffer. media_type: "video" или "image". metadata: платформо-специфичные
    поля Buffer (например {"instagram": {"type": "post", "shouldShareToFeed": True}}
    — Instagram, в отличие от TikTok, требует явно указать тип поста). Возвращает
    (ссылка_на_пост, None) при успехе или (None, причина) при ошибке/таймауте —
    причина в человекочитаемом виде от самого Buffer (например "потеряна
    авторизация канала"), не техническая ошибка. Никогда не бросает исключение."""
    if not BUFFER_API_KEY or not channel_id:
        logger.warning("BUFFER_API_KEY/channel_id не заданы — пропускаем публикацию через Buffer")
        return None, None

    headers = {"Authorization": f"Bearer {BUFFER_API_KEY}", "Content-Type": "application/json"}

    input_payload = {
        "channelId": channel_id,
        "text": caption[:2200],
        "mode": "shareNow",
        "schedulingType": "automatic",
        "assets": [{media_type: {"url": media_url}}],
    }
    if metadata:
        input_payload["metadata"] = metadata

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                BUFFER_URL,
                headers=headers,
                json={
                    "query": CREATE_POST_MUTATION,
                    "variables": {"input": input_payload},
                },
            )
            data = resp.json()
            result = data.get("data", {}).get("createPost", {})
            if "message" in result:
                logger.error(f"Buffer createPost error: {result['message']}")
                return None, result["message"]
            post_id = result.get("post", {}).get("id")
            if not post_id:
                logger.error(f"Buffer createPost: неожиданный ответ: {data}")
                return None, "неожиданный ответ Buffer"

            for _ in range(24):  # до ~2 минут ожидания публикации
                await asyncio.sleep(5)
                status_resp = await client.post(
                    BUFFER_URL,
                    headers=headers,
                    json={"query": POST_STATUS_QUERY, "variables": {"id": post_id}},
                )
                post = status_resp.json().get("data", {}).get("post", {})
                if post.get("status") == "sent":
                    logger.info(f"✅ Опубликовано через Buffer: {post['externalLink']}")
                    return post["externalLink"], None
                if post.get("status") == "error":
                    reason = (post.get("error") or {}).get("message") or "неизвестная ошибка Buffer"
                    logger.error(f"Buffer: публикация завершилась ошибкой: {reason}")
                    return None, reason

            logger.error("Buffer: публикация не завершилась за отведённое время")
            return None, "публикация не завершилась за 2 минуты"
    except Exception as e:
        logger.error(f"publish_to_buffer error: {e}")
        return None, str(e)
