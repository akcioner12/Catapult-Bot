# Hot topic selection for generated Shorts — design

## Context

`generate_daily_short` (`orchestrator.py`) picks a topic by taking `posts[0]["text"]` — the single highest-`viral_score` (views/age) post from a fixed, narrow set of Telegram channels per category (`CHANNELS` in `subagents/tg_monitor.py`, 5–9 channels per category, only 5 for `ai`). Even the best post from that narrow pool isn't necessarily the day's most resonant story in the niche — and, as the TikTok-block incident on 2026-07-11 showed, some of those channels themselves post promotional/scheme content rather than news, which the "top" pick then amplifies (see `2026-07-11-tiktok-safe-fallback-design.md` for the related fix on the publish side).

The user wants the bot to pick genuinely "hot"/resonant topics instead. Scope decided in conversation: broaden the *signal*, not the channel list — the user will supply additional Telegram channel usernames separately later; this pass adds two free, already-partially-built or free-public trending signals and changes how the topic is chosen from the combined pool.

## Architecture

### 1. New signal — CoinGecko trending coins (crypto only)

New function in `subagents/yt_ideas.py` (already home to `get_trending_shorts_ideas`, the YouTube-trending helper — same "trending signal" responsibility):

```python
async def get_trending_coins() -> list[str]:
    """Топ-7 монет с резким ростом поискового интереса (CoinGecko /search/trending,
    публичный API, без ключа). [] при любой ошибке/таймауте — как и
    get_trending_shorts_ideas, не блокирует генерацию."""
```

Hits `https://api.coingecko.com/api/v3/search/trending` (no API key required), returns up to 7 strings formatted `"{name} ({symbol})"` from the response's `coins[].item` list. Same fail-soft `try/except → []` pattern already used by `get_trending_shorts_ideas` in this file.

### 2. Candidate pool assembly (`orchestrator.py`, `generate_daily_short` — and its weekly-batch replacement, per `2026-07-11-weekly-video-scheduling-design.md`)

Replaces:
```python
posts = await collect_top_posts(category)
topic_source = posts[0]["text"] if posts else category
ideas = await get_trending_shorts_ideas(category)
if ideas:
    topic_source += "\n\nАктуальные форматы в нише сейчас: " + "; ".join(ideas[:3])
```
with a structured multi-candidate block:
```python
posts = await collect_top_posts(category)
candidates = [f"[TG] {p['text'][:200]}" for p in posts[:5]]

ideas = await get_trending_shorts_ideas(category)
candidates += [f"[YouTube] {t}" for t in ideas]

if category == "crypto":
    coins = await get_trending_coins()
    candidates += [f"[Trending coin] {c}" for c in coins]

topic_source = "\n".join(candidates) if candidates else category
```

Applies to **crypto, ai, forex**. **catapult is unchanged** — still `posts[0]["text"]` only, no `get_trending_coins`/broadened candidate pool — catapult content is self-promotion of the Catapult Trade platform, not news, so "most resonant" doesn't apply the same way.

### 3. Prompt change (`subagents/yt_script.py`, `generate_video_script`)

Current prompt (`subagents/yt_script.py:66-68`) presents `topic_source` as a single blob under "Исходный материал". Changes to make the multi-candidate structure explicit and instruct Claude to pick one:

```python
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
```

(`topic_source[:800]` → `[:1500]` — the candidate block is longer than a single post text was; 1500 chars comfortably fits 5 TG snippets (200 chars each = 1000) + up to 12 short trend lines without truncating mid-list.)

`generate_video_script`'s signature, return shape (`{"narration": ..., "image_briefs": ...}`), and `_parse_script` are unchanged — this is a prompt-content change only, not an interface change.

## Error handling

- `get_trending_coins` failure → `[]`, same as `get_trending_shorts_ideas` today — the candidate block just has fewer entries, generation proceeds.
- If `posts`, `ideas`, and (crypto) `coins` are all empty → `candidates` is `[]` → `topic_source` falls back to the bare `category` string, exactly like today's existing fallback (`topic_source = posts[0]["text"] if posts else category`).
- No new failure modes — every new call site already has an established fail-soft convention in this codebase (`get_trending_shorts_ideas`'s existing `try/except → []`, `collect_top_posts`'s existing empty-list handling).

## Out of scope for this pass

- No changes to `CHANNELS` (`subagents/tg_monitor.py`) — the user will supply additional channel usernames separately; adding them is a follow-up data change, not a code change, once received.
- No trending signal for `ai`/`forex` beyond the existing YouTube-trending helper — there's no CoinGecko-equivalent free, purpose-built "trending" API for those niches; broadening those categories further (e.g. a paid news aggregator) was explicitly deferred as Approach B in the brainstorm, not adopted this pass.
- No change to `catapult`'s topic selection.
- No change to `viral_score` or `collect_top_posts` itself — the existing TG ranking (views/age) stays as one input among several, not replaced.
