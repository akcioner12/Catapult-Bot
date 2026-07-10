"""
Sub-agent: проверка подписи видео на соответствие TikTok-ограничениям по
финансовому/крипто контенту перед публикацией — см. Task про снятое видео
"Polygate" (нарушение "Запрещённые товары и услуги").
"""
import logging
import re

import httpx

from subagents.rewriter import CLAUDE_API_KEY, CLAUDE_API_URL

logger = logging.getLogger(__name__)

FAIL_CLOSED_REASON = "не удалось проверить содержимое на соответствие правилам TikTok"


async def check_tiktok_compliance(caption: str) -> str | None:
    """None — публикация ок. Иначе — причина блокировки для сообщения админу.
    Fail-closed: любая ошибка проверки тоже трактуется как блокировка."""
    prompt = f"""Ты модератор, проверяющий подпись к видео на соответствие правилам TikTok
о финансовом/крипто контенте.

TikTok РАЗРЕШАЕТ: новости о рынке, курсах, аналитику, комментарии, образовательный контент
о крипте/форекс/ИИ — это обычный контент финансовых блогеров и медиа, его на TikTok много.
Простая ссылка на свой Telegram-канал для подписки — тоже норма, это не финансовая услуга.

TikTok ЗАПРЕЩАЕТ конкретно:
— Продвижение/рекламу конкретного трейдингового, инвестиционного или арбитражного ПРОДУКТА
  или ПЛАТФОРМЫ (например "заходи в проект X, получай очки/токены за депозит") —
  это относится и к сторонним проектам, и к собственным продуктам канала.
  Простое упоминание названия монеты/биржи в новости — это НЕ продвижение.
— Обещания гарантированной/конкретной доходности ("заработаешь 500%", "гарантированная прибыль").
— Формулировки в духе "быстро разбогатей", предложения инвестировать/вложить деньги куда-то.
— Прямые призывы к действию с деньгами (задепозить, купить, вложиться) в конкретный продукт.

Ключевой вопрос: это НОВОСТЬ/КОММЕНТАРИЙ о рынке (разрешено), или ПРИЗЫВ вложить деньги
в конкретный продукт/платформу (запрещено)? Если сомневаешься — склоняйся к OK, блокируй
только при явном нарушении.

Подпись к видео:
{caption[:1000]}

Ответь СТРОГО в этом формате, без пояснений:
VERDICT: OK
или
VERDICT: BLOCK
REASON: <одна короткая причина>"""

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
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            data = resp.json()
            if "content" not in data:
                logger.error(f"check_tiktok_compliance: Claude error: {data}")
                return FAIL_CLOSED_REASON
            answer = data["content"][0]["text"].strip()
            if answer.upper().startswith("VERDICT: OK"):
                return None
            reason_match = re.search(r"REASON:\s*(.+)", answer)
            reason = reason_match.group(1).strip() if reason_match else "нарушает правила TikTok"
            logger.info(f"TikTok compliance: BLOCK — {reason}")
            return reason
    except Exception as e:
        logger.error(f"check_tiktok_compliance error: {e}")
        return FAIL_CLOSED_REASON
