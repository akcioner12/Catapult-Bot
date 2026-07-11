"""
Sub-agent: сценарий, ТЗ для картинок и метаданные YouTube Shorts через Claude.
"""
import re
import logging

import httpx

from subagents.rewriter import CLAUDE_API_KEY, CLAUDE_API_URL
from subagents.image_brief import CATEGORY_STYLE

logger = logging.getLogger(__name__)

CONTEXT_BY_CATEGORY = {
    "crypto":   "криптовалюты, Bitcoin, блокчейн, DeFi, альткоины",
    "ai":       "искусственный интеллект, нейросети, AI инструменты для заработка",
    "forex":    "Forex, валютные пары, трейдинг, аналитика рынка",
    "catapult": "торговую платформу Catapult Trade",
}

SOCIAL_FOOTER = (
    "Telegram канал:   https://t.me/Crypto_AI_Forex\n"
    "Instagram:   https://www.instagram.com/crypto.ai.forex/\n"
    "Tik Tok:   https://www.tiktok.com/@crypto_ai_forex\n"
    "Twitter/X:   https://x.com/cryptoaiforex\n\n"
    "#Shorts #crypto #cryptocurrency #ai #aivideo #forex #forextrading #forexsignals"
)

async def _call_claude(prompt: str, max_tokens: int) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            data = resp.json()
            if "content" not in data:
                logger.error(f"Claude error: {data}")
                return None
            return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Claude call error: {e}")
        return None

# ── Сценарий для авто-озвучки ─────────────────────────────────────────────────
async def generate_video_script(topic_source: str, category: str) -> dict | None:
    style = CATEGORY_STYLE.get(category, CATEGORY_STYLE["crypto"])
    context = CONTEXT_BY_CATEGORY.get(category, "финансы")
    prompt = f"""Ты — автор вертикальных YouTube Shorts для канала «Крипта, AI, Forex. Как заработать?» (тот же канал, что и в Telegram @Crypto_AI_Forex).

Сценарий пишется для озвучки диктором (TTS) — только то, что должно прозвучать. Без эмодзи, без HTML-тегов, без ремарок в скобках.
Стиль: живо, по делу, крючок в первые 2 секунды, 90-150 слов (30-60 секунд речи).

Тема: {context}
Стиль картинок: {style}

Кандидаты на тему за последние часы (посты из каналов, тренды YouTube Shorts{', резко растущие монеты' if category == 'crypto' else ''}):
{topic_source[:1500]}

Выбери ОДНУ самую резонансную, горячую историю из кандидатов выше и напиши сценарий именно про неё — не пытайся смешать несколько тем в одну. Если ни один кандидат не выглядит по-настоящему интересным, возьми {context} как тему в целом.

Напиши сценарий ролика и 2-4 ТЗ для картинок, которые будут сменять друг друга под озвучку — каждое ТЗ должно соответствовать стилю картинок выше.

Ответь СТРОГО в этом формате, без пояснений:
SCRIPT:
<текст для озвучки>
IMAGE 1: <ТЗ для картинки одним предложением>
IMAGE 2: <ТЗ для картинки одним предложением>
IMAGE 3: <ТЗ для картинки одним предложением>"""

    raw = await _call_claude(prompt, max_tokens=800)
    if not raw:
        return None
    return _parse_script(raw)

def _parse_script(raw: str) -> dict | None:
    script_match = re.search(r"SCRIPT:\s*(.+?)(?=\nIMAGE \d+:|\Z)", raw, re.DOTALL)
    image_matches = re.findall(r"IMAGE \d+:\s*(.+)", raw)
    if not script_match or not image_matches:
        logger.error(f"Не удалось распарсить сценарий: {raw[:300]}")
        return None
    narration = script_match.group(1).strip()
    image_briefs = [m.strip() for m in image_matches]
    if not narration or not image_briefs:
        return None
    return {"narration": narration, "image_briefs": image_briefs}

# ── Сценарий для самозаписи ───────────────────────────────────────────────────
async def generate_self_record_script(category: str) -> dict | None:
    context = CONTEXT_BY_CATEGORY.get(category, "финансы")
    prompt = f"""Ты — автор вертикальных YouTube Shorts для канала «Крипта, AI, Forex. Как заработать?».

Тема: {context}

Придумай тему и напиши сценарий на 30-60 секунд, который автор канала прочитает на камеру сам (живая речь от первого лица, не диктор TTS).
Живо, разговорным языком, крючок в первые 2 секунды.

Ответь СТРОГО в этом формате, без пояснений:
TOPIC: <тема одной строкой>
SCRIPT:
<текст для начитки>"""

    raw = await _call_claude(prompt, max_tokens=500)
    if not raw:
        return None
    topic_match = re.search(r"TOPIC:\s*(.+)", raw)
    script_match = re.search(r"SCRIPT:\s*(.+)", raw, re.DOTALL)
    if not topic_match or not script_match:
        logger.error(f"Не удалось распарсить self-record сценарий: {raw[:300]}")
        return None
    topic = topic_match.group(1).strip()
    script = script_match.group(1).strip()
    if not topic or not script:
        return None
    return {"topic": topic, "script": script}

# ── Метаданные для загрузки на YouTube ────────────────────────────────────────
async def generate_video_metadata(topic: str, script_text: str, category: str) -> dict | None:
    prompt = f"""Ты — автор YouTube Shorts канала «Крипта, AI, Forex. Как заработать?».

Тема ролика: {topic}
Текст ролика: {script_text[:800]}

Напиши для загрузки на YouTube:
1. Название — до 100 символов, цепляющее, без обманного кликбейта
2. Описание — 2-3 предложения + призыв подписаться на Telegram @Crypto_AI_Forex
3. 5-8 тегов через запятую (без #, просто ключевые слова)

Ответь СТРОГО в этом формате:
TITLE: <название>
DESCRIPTION: <описание>
TAGS: <тег1, тег2, тег3>"""

    raw = await _call_claude(prompt, max_tokens=400)
    if not raw:
        return None
    title_match = re.search(r"TITLE:\s*(.+)", raw)
    desc_match = re.search(r"DESCRIPTION:\s*(.+?)(?=\nTAGS:|\Z)", raw, re.DOTALL)
    tags_match = re.search(r"TAGS:\s*(.+)", raw)
    if not title_match or not desc_match:
        logger.error(f"Не удалось распарсить метаданные видео: {raw[:300]}")
        return None
    title = title_match.group(1).strip()[:100]
    description = desc_match.group(1).strip() + "\n\n" + SOCIAL_FOOTER
    tags = [t.strip() for t in tags_match.group(1).split(",")] if tags_match else []
    if not title or not description:
        return None
    return {"title": title, "description": description, "tags": tags}
