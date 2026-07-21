"""
Sub-agent: подсказки для ручного engagement (лайки/комментарии/подписки от
живого человека под чужими тематическими видео в TikTok/Instagram) — темы
берутся из тех же источников, что и для генерации собственного контента,
конкретные видео/авторов бот не ищет (площадка не даёт официального API для
этого), просто предлагает готовый текст комментария под тему.
"""
import logging
import re

import httpx

from subagents.rewriter import CLAUDE_API_KEY, CLAUDE_API_URL

logger = logging.getLogger(__name__)


async def generate_engagement_comments(topic_source: str, category: str) -> list[str]:
    """2 коротких варианта комментария под тему. [] при сбое — не блокирует дайджест."""
    prompt = f"""Ты ведёшь Instagram/TikTok-аккаунт на тему крипты/форекса/ИИ/трейдинга.

Ниже — горячая тема дня в нише {category}:
{topic_source[:700]}

Напиши 2 коротких варианта комментария, которые можно оставить под ЧУЖИМ видео на эту тему
(в TikTok или Instagram), чтобы его автор и зрители заметили тебя и захотели зайти на твой профиль.

Требования к каждому варианту:
— По существу темы, а не общая фраза ("класс!", "огонь!") — покажи, что ты реально понимаешь тему.
— Добавляет что-то своё: мнение, факт, лёгкий инсайт или уточняющий вопрос.
— БЕЗ ссылок, БЕЗ "подписывайся", БЕЗ прямой рекламы — это выглядит как спам и отталкивает.
— 1-2 предложения, разговорный тон, без хэштегов.

Ответь СТРОГО в этом формате, без пояснений:
1: <текст>
2: <текст>"""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            data = resp.json()
            if "content" not in data:
                logger.error(f"generate_engagement_comments error: {data}")
                return []
            raw = data["content"][0]["text"]
            comments = re.findall(r"^\s*\d+[.:)]\s*(.+)$", raw, re.MULTILINE)
            if not comments:
                logger.warning(f"generate_engagement_comments[{category}]: не удалось распарсить ответ: {raw[:300]!r}")
            return comments
    except Exception as e:
        logger.error(f"generate_engagement_comments error: {e}")
        return []
