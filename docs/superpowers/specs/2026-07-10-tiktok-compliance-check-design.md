# TikTok compliance check before publish — design

## Context

TikTok removed a real published video ("Polygate: арбитраж на Polymarket...", category `crypto`) for violating community guidelines ("Запрещенные товары и услуги"). Research confirmed: TikTok restricts financial services/cryptocurrency content broadly, bans promotion of specific unlicensed trading platforms, guaranteed-return claims, and "get rich quick" framing — regardless of exact wording. Repeated violations risk permanent account suspension. The user wants generated content checked against these restrictions before it reaches TikTok, with TikTok skipped (not YouTube) when a video would violate them.

## Architecture

New module `subagents/tiktok_moderation.py`:

```python
async def check_tiktok_compliance(caption: str) -> str | None:
    """Проверяет caption на соответствие TikTok-ограничениям по финансовому/крипто
    контенту. Возвращает None если публикация ок, иначе — причину блокировки
    (человекочитаемую, для сообщения админу). Fail-closed: ошибка самой проверки
    (например, недоступен Claude) тоже возвращает причину, а не None — риск бана
    важнее доступности."""
```

Same calling convention as the existing `_call_claude`-style functions in `yt_script.py` (direct `httpx` POST to `api.anthropic.com`, no shared helper — matches this codebase's existing per-file pattern), using `CLAUDE_API_KEY`/`CLAUDE_API_URL` from `subagents.rewriter` like `yt_script.py` and `image_brief.py` already do.

Prompt asks Claude to evaluate the caption against a concise summary of the relevant restrictions (financial services/cryptocurrency content is heavily restricted; no promotion of a specific unlicensed trading/arbitrage platform; no guaranteed-return or "get rich quick" claims) and answer strictly `VERDICT: OK` or `VERDICT: BLOCK` + `REASON: <one sentence>`.

## Data flow

In `_finish_publish` (`subagents/yt_publisher.py`), before the existing `upload_to_tiktok` call:

1. If `video["category"] == "catapult"`: skip TikTok deterministically, **no Claude call** — this category is always a promotion of the Catapult Trade platform itself, unconditionally in the restricted zone.
2. Otherwise: call `check_tiktok_compliance(_tiktok_caption(video))`.
3. If a block reason came back (from either path): add `⚠️ TikTok пропущен: {reason}` to the admin status message, do **not** call `upload_to_tiktok`, do **not** add the video to `tiktok_retry_pending` (retrying would just re-hit the same category rule or the same Claude verdict on unchanged text — not useful).
4. If no block reason: proceed exactly as today (call `upload_to_tiktok`, existing success/failure/retry-pending handling unchanged).

This means `tiktok_retry_pending` now exclusively holds genuine upload-mechanism failures (network error, Buffer API error) — compliance skips never enter it, so `/retry_tiktok`'s existing "retry the same call" semantics stay meaningful.

## Error handling

- `check_tiktok_compliance` never raises — same convention as the rest of `subagents/`. Any exception (Claude API down, malformed response) is treated as a block, with a generic reason ("не удалось проверить содержимое на соответствие правилам TikTok") rather than allowing the video through — this is a deliberate fail-closed choice given the cost of a wrong "OK" (repeated strikes risking permanent ban) vastly exceeds the cost of a wrong "BLOCK" (one video only reaches YouTube, easily fixed by editing and manually posting to TikTok if the block was a false positive).

## Out of scope for this pass

- No automatic rewriting to a "safe" neutral version — a blocked video's TikTok leg is simply skipped, per the user's explicit choice.
- No visual/video-content analysis — text-only (the caption actually being posted), since nothing in this pipeline currently analyzes video frames.
- No moderation for YouTube or the Telegram channel post — YouTube's restrictions on this content type are looser and out of scope; this check exists specifically because of TikTok's ban risk.
