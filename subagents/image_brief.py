"""
Sub-agent: ТЗ для картинки на основе текста поста.
Перенесено из parser.py без изменения логики.
"""
import logging

import httpx

from subagents.rewriter import CLAUDE_API_KEY, CLAUDE_API_URL

logger = logging.getLogger(__name__)

# ── Стиль по категориям (используется и для видео-ТЗ в yt_script.py) ─────────
CATEGORY_STYLE = {
    "crypto":   "тёмный фон, неоновые синие и оранжевые цвета, Bitcoin/крипто символика, торговые графики",
    "ai":       "тёмный фон, фиолетовые и голубые цвета, нейронные сети, цифровые паттерны",
    "forex":    "тёмный фон, зелёные и синие цвета, валютные пары, торговые графики",
    "catapult": "тёмный фон, золотые и оранжевые цвета, ракета/запуск, трейдинг платформа",
}

# ── ТЗ для картинки ───────────────────────────────────────────────────────────
async def generate_image_brief(post_text: str, category: str) -> str:
    style = CATEGORY_STYLE.get(category, CATEGORY_STYLE["crypto"])
    prompt = f"""На основе этого поста составь короткое ТЗ для дизайнера/Midjourney на создание картинки.

Пост:
{post_text[:500]}

Стиль: {style}, размер 1200x630px, кинематографично, фотореалистично.

Напиши ТЗ в 2-3 предложения: что должно быть на картинке, цвета, настроение.
Пиши простым текстом без markdown, без звёздочек, без заголовков. Только текст."""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            data = resp.json()
            if "content" in data:
                return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Brief error: {e}")
    return f"Фотореалистичная картинка на тему {category}, тёмный фон, неоновые цвета, 1200x630px."
