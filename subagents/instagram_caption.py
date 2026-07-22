"""
Sub-agent: SEO-подпись и хэштеги для Instagram (лента и Reels) через Claude.
"""
import re
import logging

import httpx

from subagents.rewriter import CLAUDE_API_KEY, CLAUDE_API_URL

logger = logging.getLogger(__name__)

CONTENT_TYPE_STYLE = {
    "photo": "чуть длиннее, можно 2-4 предложения, разговорный тон",
    "reel": "коротко и энергично, 1-2 предложения, крючок в первой строке",
}


async def generate_instagram_caption(source_text: str, category: str, content_type: str) -> dict:
    """content_type: "photo" или "reel". Возвращает {"caption": str, "hashtags": list[str]}.
    При сбое Claude или парсинга — возвращает исходный текст как caption и
    пустой список хэштегов (публикация всё равно пройдёт, просто без SEO)."""
    style = CONTENT_TYPE_STYLE.get(content_type, CONTENT_TYPE_STYLE["photo"])
    catapult_guard = (
        "\nИсходный текст может содержать рекламную подачу (реферальный заработок, "
        "\"лучший момент войти\", обещания доходности, личные заявления о заработке) — "
        "для Instagram её НЕ наследуй, перепиши нейтрально: только факты/новости "
        "платформы, БЕЗ обещаний прибыли, БЕЗ срочности, БЕЗ реферальных заработков, "
        "БЕЗ прямых призывов зарегистрироваться или вложить деньги. Instagram уже "
        "ограничивал аккаунт за такие формулировки как признак мошенничества.\n"
    ) if category == "catapult" else ""
    prompt = f"""Ты ведёшь Instagram-аккаунт {category}-тематики (крипта/финансы/ИИ) на русском языке.

Исходный текст:
{source_text[:1500]}
{catapult_guard}
Напиши подпись для Instagram под этот контент — {style}. Без эмодзи через каждое слово, без markdown, без заголовков.
Подбери 10-15 хэштегов: смесь широких (#crypto, #bitcoin), нишевых по теме поста и брендовых (#catapulttrade).

Ответь СТРОГО в этом формате, без пояснений:
CAPTION:
<текст подписи>
HASHTAGS: #tag1 #tag2 #tag3 ..."""

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
                    "max_tokens": 400,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            data = resp.json()
            if "content" not in data:
                logger.error(f"Instagram caption Claude error: {data}")
                return {"caption": source_text[:2000], "hashtags": []}
            raw = data["content"][0]["text"]
    except Exception as e:
        logger.error(f"generate_instagram_caption error: {e}")
        return {"caption": source_text[:2000], "hashtags": []}

    return _parse_caption(raw, source_text)


def _parse_caption(raw: str, fallback_text: str) -> dict:
    caption_match = re.search(r"CAPTION:\s*(.+?)(?=\nHASHTAGS:|\Z)", raw, re.DOTALL)
    hashtags_match = re.search(r"HASHTAGS:\s*(.+)", raw)
    if not caption_match:
        logger.error(f"Не удалось распарсить подпись Instagram: {raw[:300]}")
        return {"caption": fallback_text[:2000], "hashtags": []}
    caption = caption_match.group(1).strip()
    hashtags = re.findall(r"#\w+", hashtags_match.group(1)) if hashtags_match else []
    return {"caption": caption, "hashtags": hashtags}
