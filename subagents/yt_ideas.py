"""
Sub-agent: подсказки по темам для YouTube Shorts на основе того, что заходит в нише.
Только чтение — никогда не скачивает и не переиспользует чужие видео/аудио,
только заголовки как вдохновение для темы.
"""
import asyncio
import logging
import httpx

logger = logging.getLogger(__name__)

SEARCH_KEYWORDS = {
    "crypto":   "криптовалюта",
    "ai":       "искусственный интеллект заработок",
    "forex":    "форекс трейдинг",
    "catapult": "crypto трейдинг платформа",
}

COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"

def _search_sync(youtube, query: str) -> list:
    response = youtube.search().list(
        part="snippet",
        q=query,
        type="video",
        videoDuration="short",
        order="viewCount",
        maxResults=5,
        relevanceLanguage="ru",
    ).execute()
    return [item["snippet"]["title"] for item in response.get("items", [])]

async def get_trending_shorts_ideas(category: str) -> list:
    from subagents.yt_publisher import get_youtube_service

    query = SEARCH_KEYWORDS.get(category, category)
    try:
        youtube = get_youtube_service()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _search_sync, youtube, query)
    except Exception as e:
        logger.warning(f"get_trending_shorts_ideas error: {e}")
        return []


async def get_trending_coins() -> list[str]:
    """Топ монет с резким ростом поискового интереса (CoinGecko /search/trending,
    публичный API, без ключа). [] при любой ошибке/таймауте — не блокирует генерацию."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(COINGECKO_TRENDING_URL)
            data = resp.json()
            coins = data.get("coins", [])[:7]
            return [f'{c["item"]["name"]} ({c["item"]["symbol"].upper()})' for c in coins]
    except Exception as e:
        logger.warning(f"get_trending_coins error: {e}")
        return []
