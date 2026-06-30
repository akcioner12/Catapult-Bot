"""
Sub-agent: мониторинг и скоринг постов из Telegram-каналов конкурентов.
Перенесено из parser.py без изменения логики.
"""
import os
import hashlib
import asyncio
import logging
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TGSTAT_TOKEN     = os.getenv("TGSTAT_TOKEN", "")
TGSTAT_API_URL   = "https://api.tgstat.ru"
TOP_POSTS        = 5

# ── Каналы ────────────────────────────────────────────────────────────────────
CHANNELS = {
    "crypto": [
        "crypto_Iemon", "to_the_makemoney", "airolejon",
        "eeusd", "if_crypto_ru", "cryptomedwed",
        "cryptanci", "DeCenter", "cointelegraph"
    ],
    "ai": [
        "neurobussines", "naebnet", "neyroseti_dr", "loading100ai", "king_ai"
    ],
    "forex": [
        "PROFiInvest", "tradeforexexchange", "premiumgolubev",
        "markoptions", "newwavetrade", "goldenonemoney", "uiartemzvezdin"
    ],
    "catapult": [
        "letsCatapult", "to_the_makemoney", "airolejon", "catapult_community"
    ]
}

# ── Состояние ─────────────────────────────────────────────────────────────────
sent_hashes: set = set()

# ── Хэш ───────────────────────────────────────────────────────────────────────
def make_hash(text: str) -> str:
    return hashlib.md5(text[:200].encode()).hexdigest()

# ── TGStat API ────────────────────────────────────────────────────────────────
async def get_posts_tgstat(channel: str) -> list:
    if not TGSTAT_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{TGSTAT_API_URL}/channels/posts",
                params={
                    "token": TGSTAT_TOKEN,
                    "channelId": f"@{channel}",
                    "limit": 10,
                    "extended": 1
                }
            )
            data = resp.json()
            if data.get("status") != "ok":
                return []
            items = data.get("response", {}).get("items", [])
            posts = []
            for item in items:
                text = item.get("text", "").strip()
                views = item.get("viewsCount", 0) or 0
                if len(text) > 100:
                    h = make_hash(text)
                    if h not in sent_hashes:
                        posts.append({"text": text, "channel": channel, "views": views, "hash": h, "source": "tgstat"})
            return posts
    except Exception as e:
        logger.warning(f"TGStat error @{channel}: {e}")
        return []

def parse_post_date(msg) -> datetime | None:
    """Парсим дату поста из HTML"""
    try:
        time_tag = msg.find_parent("div", class_="tgme_widget_message").find("time")
        if time_tag and time_tag.get("datetime"):
            from datetime import timezone
            dt = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        pass
    return None

def parse_post_views(msg) -> int:
    """Парсим просмотры поста"""
    try:
        wrap = msg.find_parent("div", class_="tgme_widget_message")
        views_tag = wrap.find("span", class_="tgme_widget_message_views")
        if views_tag:
            v = views_tag.get_text().strip().replace("K", "000").replace("M", "000000")
            return int("".join(filter(str.isdigit, v)))
    except Exception:
        pass
    return 0

async def get_posts_web(channel: str, hours: int = 24) -> list:
    posts = []
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=15
        ) as client:
            resp = await client.get(f"https://t.me/s/{channel}")
            if resp.status_code != 200:
                return []
            soup = BeautifulSoup(resp.text, "html.parser")
            msgs = soup.find_all("div", class_="tgme_widget_message_text")
            cutoff = datetime.utcnow() - timedelta(hours=hours)

            for msg in msgs:
                text = msg.get_text(separator="\n").strip()
                if len(text) < 100:
                    continue
                h = make_hash(text)
                if h in sent_hashes:
                    continue
                post_date = parse_post_date(msg)
                # Если дата не распарсилась — берём пост (на всякий случай)
                if post_date and post_date < cutoff:
                    continue
                views = parse_post_views(msg)
                posts.append({
                    "text": text,
                    "channel": channel,
                    "views": views,
                    "hash": h,
                    "source": "web",
                    "date": post_date
                })
    except Exception as e:
        logger.warning(f"Web error @{channel}: {e}")
    return posts

def viral_score(post: dict) -> float:
    """Скор вовлечённости с учётом свежести: просмотры / (часы + 1)"""
    views = post.get("views", 0) or 0
    post_date = post.get("date")
    if post_date:
        age_hours = max(0, (datetime.utcnow() - post_date).total_seconds() / 3600)
    else:
        age_hours = 12  # если дата неизвестна — считаем средний возраст
    return views / (age_hours + 1)

async def collect_top_posts(category: str) -> list:
    channels = CHANNELS.get(category, [])
    all_posts = []

    for channel in channels:
        # Сначала пробуем TGStat
        posts = await get_posts_tgstat(channel)
        if not posts:
            # Парсим за 24 часа
            posts = await get_posts_web(channel, hours=24)
            # Если мало постов — расширяем до 48 часов
            if len(posts) < 2:
                posts = await get_posts_web(channel, hours=48)
        all_posts.extend(posts)
        await asyncio.sleep(0.5)

    # Сортируем по вирусному скору (просмотры / возраст)
    all_posts.sort(key=viral_score, reverse=True)
    combined = all_posts[:TOP_POSTS]

    for p in combined:
        score = viral_score(p)
        age = round((datetime.utcnow() - p["date"]).total_seconds() / 3600, 1) if p.get("date") else "?"
        logger.info(f"  [{category}] @{p['channel']} | 👁{p['views']} | ⏱{age}ч | скор={score:.1f}")

    logger.info(f"[{category}] Итого: {len(combined)} постов (из {len(all_posts)})")
    return combined
