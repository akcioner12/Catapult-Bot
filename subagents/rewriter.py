"""
Sub-agent: переделка найденного контента "под свой стиль" через Claude API.
Перенесено из parser.py без изменения логики.
"""
import os
import re
import logging
from datetime import datetime

import httpx

from subagents.tg_monitor import viral_score

logger = logging.getLogger(__name__)

CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_API_URL   = "https://api.anthropic.com/v1/messages"

# ── Углы для Catapult ─────────────────────────────────────────────────────────
CATAPULT_ANGLES = [
    "реферальная программа и заработок на команде",
    "поинты и токены — выгода раннего входа",
    "личный опыт — что я уже накопил и заработал",
    "сравнение с другими платформами — почему Catapult лучше",
    "инструкция как зарегистрироваться и начать",
    "результаты команды — цифры и динамика",
    "ответы на частые вопросы о проекте",
]

# ── Claude API — генерация поста ──────────────────────────────────────────────
STYLE_GUIDE = """Ты — автор Telegram канала «Крипта, AI, Forex. Как заработать?».

Твой стиль:
- Начинаешь с 👋 Друзья, ... или 👋 Друзья, всем привет! или 👋 Друзья, приветствую!
- Каждый абзац начинается с тематического эмодзи
- Пишешь от первого лица, живо и практично
- 150-250 слов
- В конце всегда призыв к действию
- НЕ копируешь дословно — пересказываешь своими словами

ВАЖНО — форматирование ТОЛЬКО через HTML теги Telegram:
- жирный: <b>текст</b>
- курсив: <i>текст</i>
- цитата: <blockquote>текст</blockquote>
- НИКАКИХ звёздочек **текст** — это не работает в Telegram!
- НИКАКОГО markdown форматирования!"""

async def generate_post_claude(posts: list, category: str) -> str:
    context = {
        "crypto": "криптовалюты, Bitcoin, блокчейн, DeFi, альткоины",
        "ai":     "искусственный интеллект, нейросети, AI инструменты для заработка",
        "forex":  "Forex, валютные пары, трейдинг, аналитика рынка",
        "catapult": "торговую платформу Catapult Trade — новости, обновления, партнёрства, акции платформы"
    }

    # Формируем дайджест из всех постов с метриками
    news_digest = ""
    for i, p in enumerate(posts, 1):
        age = ""
        if p.get("date"):
            age_hours = round((datetime.utcnow() - p["date"]).total_seconds() / 3600, 1)
            age = f"⏱{age_hours}ч назад"
        score = round(viral_score(p), 1)
        news_digest += (
            f"\n--- Новость {i} (@{p['channel']} | 👁{p['views']} просмотров | {age} | скор вирусности={score}) ---\n"
            f"{p['text'][:600]}\n"
        )

    prompt = f"""{STYLE_GUIDE}

Тема: {context.get(category, 'финансы')}

Ниже {len(posts)} свежих постов за последние 24-48 часов из телеграм каналов по теме {category}.
У каждого поста указаны: просмотры, возраст и скор вирусности (просмотры/часы — чем выше, тем горячее).

Выбери САМУЮ горячую и резонансную тему — учитывай скор вирусности и свежесть.
Свежий пост с высоким скором важнее старого с большими просмотрами.
Напиши на её основе один пост для канала с HTML форматированием (теги: <b>, <i>, <blockquote>).
НЕ копируй дословно — осмысли и перескажи своими словами.

{news_digest}

{"В конце добавь: 👉 Подробнее в боте: @catapulttrade_guide_bot" if category == "catapult" else "В конце добавь: 💰 Лучший заработок сегодня здесь: @catapulttrade_guide_bot"}

Только готовый пост, без пояснений."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            data = resp.json()
            if "content" in data:
                return data["content"][0]["text"]
            logger.error(f"Claude error: {data}")
            return ""
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return ""

# ── Claude API — пост о Catapult ──────────────────────────────────────────────
async def generate_catapult_post(angle: str) -> str:
    prompt = f"""{STYLE_GUIDE}

Напиши пост о торговой платформе Catapult Trade.

Угол: {angle}

Факты о Catapult Trade:
- Торговая платформа где каждая сделка приносит поинты
- Поинты конвертируются в токены платформы при листинге
- Проект на ранней стадии — лучший момент для входа
- Реферальная программа — % от активности команды
- Бот с подробностями: @catapulttrade_guide_bot

Напиши живой пост от первого лица с HTML форматированием.
В конце: 🤖 Все подробности → @catapulttrade_guide_bot

Только готовый пост, без пояснений."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            data = resp.json()
            if "content" in data:
                return data["content"][0]["text"]
            return "Пост о Catapult"
    except Exception as e:
        logger.error(f"Claude Catapult error: {e}")
        return "Пост о Catapult"

# ── Claude API — генерация опроса ──────────────────────────────────────────────
async def generate_poll(recent_questions: list) -> dict | None:
    """Придумывает новый опрос (вопрос + варианты) на тему крипты/форекса/ИИ/Catapult.
    Возвращает None при сбое — вызывающий код падает обратно на статический список."""
    avoid = "\n".join(f"- {q}" for q in recent_questions[-15:]) if recent_questions else "(пока нет истории)"
    prompt = f"""Ты придумываешь опросы (голосования) для Telegram-канала о крипте, форексе, ИИ-заработке и платформе Catapult Trade.

Придумай ОДИН новый опрос на любую из этих тем — сам выбери, что сейчас интереснее аудитории.

Требования:
- Вопрос живой и цепляющий, не банальный ("как дела" не подходит) — про конкретные привычки, стратегии, мнения, страхи или опыт аудитории в трейдинге/крипте/заработке с ИИ.
- Ровно 4 коротких варианта ответа (2-5 слов каждый).
- НЕ повторяй и не перефразируй уже использованные вопросы (список ниже) — придумай реально новый угол:
{avoid}

Ответь СТРОГО в этом формате, без пояснений:
ВОПРОС: <текст вопроса с эмодзи в начале>
1. <вариант>
2. <вариант>
3. <вариант>
4. <вариант>"""

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
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            data = resp.json()
            text = data["content"][0]["text"]
            question = ""
            options = []
            for line in text.strip().split("\n"):
                line = line.strip()
                if line.upper().startswith("ВОПРОС:"):
                    question = line.split(":", 1)[1].strip()
                    continue
                m = re.match(r"^\d+[.\)]\s*(.+)", line)
                if m:
                    options.append(m.group(1).strip())
            if question and len(options) >= 2:
                return {"question": question, "options": options[:10]}
            logger.warning(f"generate_poll: не удалось распарсить ответ: {text[:200]}")
            return None
    except Exception as e:
        logger.error(f"generate_poll error: {e}")
        return None
