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


async def generate_engagement_idea(topic_source: str, category: str) -> dict | None:
    """{"query": str, "comments": [str, str]} под широкую тему (не узкий факт из
    исходного поста — под конкретную цифру/событие видео в TikTok/Instagram может
    и не найтись). None при сбое — не блокирует дайджест."""
    prompt = f"""Ты ведёшь Instagram/TikTok-аккаунт на тему крипты/форекса/ИИ/трейдинга.

Ниже — горячая тема дня в нише {category} (из Telegram-канала, конкретные цифры и детали
могут не встретиться в TikTok/Instagram — это просто наводка на общую тему):
{topic_source[:700]}

Сделай две вещи:

1. QUERY: короткий поисковый запрос (2-4 слова) для поиска в TikTok/Instagram — широкая
тема, не узкий факт, чтобы по нему реально нашлись существующие видео (например не
"зона 49.70-54.50 по серебру", а "серебро форекс" или "цена серебра").

2. Два коротких варианта комментария на эту широкую тему — которые можно оставить под
РАЗНЫМИ подходящими видео по этому запросу (не только под одно конкретное видео с
той самой цифрой из источника), чтобы автор и зрители заметили тебя и зашли на профиль.

Требования к комментариям:
— По существу темы, а не общая фраза ("класс!", "огонь!") — покажи, что разбираешься.
— Добавляет что-то своё: мнение, наблюдение или уточняющий вопрос — без привязки к цифрам,
которых может не быть в найденном видео.
— БЕЗ ссылок, БЕЗ "подписывайся", БЕЗ прямой рекламы — выглядит как спам и отталкивает.
— 1-2 предложения, разговорный тон, без хэштегов.

Ответь СТРОГО в этом формате, без пояснений:
QUERY: <запрос>
1: <комментарий>
2: <комментарий>"""

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
                    "max_tokens": 350,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            data = resp.json()
            if "content" not in data:
                logger.error(f"generate_engagement_idea error: {data}")
                return None
            raw = data["content"][0]["text"]
            query_match = re.search(r"QUERY:\s*(.+)", raw)
            comments = re.findall(r"^\s*\d+[.:)]\s*(.+)$", raw, re.MULTILINE)
            if not query_match or not comments:
                logger.warning(f"generate_engagement_idea[{category}]: не удалось распарсить ответ: {raw[:300]!r}")
                return None
            return {"query": query_match.group(1).strip(), "comments": comments}
    except Exception as e:
        logger.error(f"generate_engagement_idea error: {e}")
        return None
