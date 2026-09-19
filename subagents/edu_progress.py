"""
Прогресс обучающей программы. Один урок на рубрику в день: пост, второй пост
и видео этого дня получают ОДИН И ТОТ ЖЕ урок (день = дата публикации,
YYYY-MM-DD). bonus=True — отдельный дополнительный урок в тот же день
(forex-пост 11:00 по вт/пт). Состояние переживает рестарты (/data).
"""
import json
import logging
import os

from subagents.edu_curriculum import CURRICULUM

logger = logging.getLogger(__name__)

PROGRESS_FILE = "/data/edu_progress.json"
EXTEND_COUNT = 5


def _load(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
                if isinstance(state, dict):
                    return state
    except Exception as e:
        logger.error(f"edu_progress load error: {e}")
    return {}


def _save(state: dict, path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"edu_progress save error: {e}")


def _bank(state: dict, category: str) -> list:
    return CURRICULUM[category] + state.get("extra", {}).get(category, [])


def peek_or_assign(category: str, day: str, bonus: bool = False, path: str = PROGRESS_FILE) -> dict | None:
    state = _load(path)
    bank = _bank(state, category)
    assigned = state.setdefault("assigned", {}).setdefault(category, {})
    key = f"{day}:bonus" if bonus else day
    if key in assigned:
        idx = assigned[key]
    else:
        idx = state.setdefault("next", {}).get(category, 0)
        if idx >= len(bank):
            return None
        assigned[key] = idx
        state["next"][category] = idx + 1
        _save(state, path)
    return {**bank[idx], "number": idx + 1}


async def extend_bank(category: str, path: str = PROGRESS_FILE) -> bool:
    """Банк исчерпан — просим Claude придумать следующие уроки того же уровня."""
    from subagents.yt_script import _call_claude
    state = _load(path)
    bank = _bank(state, category)
    done_titles = "\n".join(f"- {l['title']}" for l in bank[-30:])
    raw = await _call_claude(
        f"""Ты методист обучающего канала по теме «{category}» (крипта/forex/ИИ).
Уже пройденные уроки (последние):
{done_titles}

Придумай {EXTEND_COUNT} СЛЕДУЮЩИХ уроков продвинутого уровня, не повторяя пройденные.
Ответь СТРОГО строками формата: заголовок | что раскрыть (одно предложение). Без нумерации и пояснений.""",
        max_tokens=600,
    )
    if not raw:
        return False
    extra = state.setdefault("extra", {}).setdefault(category, [])
    added = 0
    for line in raw.splitlines():
        if "|" not in line:
            continue
        title, points = (p.strip(" -•\t") for p in line.split("|", 1))
        if title and points:
            extra.append({"id": f"{category}x{len(extra) + 1}", "level": 3, "title": title, "points": points})
            added += 1
    if added:
        _save(state, path)
    return added > 0


async def get_lesson(category: str, day: str, bonus: bool = False, path: str = PROGRESS_FILE) -> dict | None:
    lesson = peek_or_assign(category, day, bonus, path)
    if lesson is None and await extend_bank(category, path):
        lesson = peek_or_assign(category, day, bonus, path)
    return lesson
