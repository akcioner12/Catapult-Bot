"""
Sub-agent: воскресный контент-план.
Перенесено из parser.py. Note: send_weekly_plan takes bot_token/admin_id as
parameters instead of reading module globals, since those now live in parser.py.
"""
import logging

import httpx

from subagents.rewriter import CLAUDE_API_KEY, CLAUDE_API_URL

logger = logging.getLogger(__name__)

# ── Воскресный контент-план ───────────────────────────────────────────────────
async def generate_weekly_plan() -> str:
    prompt = f"""Составь контент-план на неделю для Telegram канала «Крипта, AI, Forex. Как заработать?».

Расписание каждого дня:
09:00 — Крипта
11:00 — Catapult Trade
13:00 — ИИ
15:00 — Catapult Trade
16:30 — Опрос
18:00 — Форекс
20:00 — Крипта

Напиши план на 7 дней (Пн-Вс). Для каждого поста укажи конкретную тему/идею.
Формат: день → время → тема одной строкой.
Используй эмодзи. Без лишних слов."""

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
                    "max_tokens": 1500,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            data = resp.json()
            if "content" in data:
                return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Weekly plan error: {e}")
    return "⚠️ Не удалось сгенерировать контент-план."

async def send_weekly_plan(bot_token: str, admin_id: int):
    logger.info("=== Воскресный контент-план ===")
    plan = await generate_weekly_plan()
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": admin_id,
                "text": f"📅 <b>КОНТЕНТ-ПЛАН НА НЕДЕЛЮ</b>\n\n{plan}",
                "parse_mode": "HTML"
            }
        )
