# TikTok-safe fallback generation — design

## Context

Two consecutive test videos today were correctly blocked by `check_tiktok_compliance` for genuine reasons (one promoted a specific third-party crypto product, one pushed a specific money-making scheme with a link) — the check is working as designed (see `2026-07-10-tiktok-compliance-check-design.md`). Root cause traced to topic selection pulling verbatim from source posts that are themselves promotional, which the separate "hot topic selection" work (`2026-07-11-hot-topic-selection-design.md`) addresses going forward.

Even after that fix, some topics will still legitimately trip TikTok's stricter content rules while being completely fine for YouTube (TikTok is more restrictive about financial/crypto framing than YouTube). Today, a block means TikTok is skipped entirely for that video — one lost TikTok post per block. The user wants a second chance: when the original video would be blocked, generate an alternate, more neutral/informational version of the same story specifically for TikTok, reusing the already-generated images, instead of giving up on TikTok for that topic.

This also removes catapult's current special case: today `_finish_publish` skips TikTok for `category == "catapult"` deterministically, no Claude call, because it's *always* self-promotion of the Catapult Trade platform. The user wants catapult to get the same fallback chance as everything else — if a more informational framing of a catapult story can pass the compliance check, let it try.

## Architecture

### 1. `_finish_publish` (subagents/yt_publisher.py) — trigger point

```python
async def _finish_publish(video_id: str, video: dict, youtube_id: str):
    await announce_in_telegram(youtube_id, video["title"], video.get("thumbnail_path"))

    status_lines = [f"✅ YouTube: https://youtu.be/{youtube_id}"]
    block_reason = await check_tiktok_compliance(_tiktok_caption(video))   # catapult no longer special-cased here

    if block_reason:
        fallback_result = await _attempt_tiktok_fallback(video_id, video, block_reason)
        if fallback_result:
            tiktok_url, note = fallback_result
            status_lines.append(f"✅ TikTok: {tiktok_url} ({note})")
        else:
            status_lines.append(f"⚠️ TikTok пропущен: {block_reason}")
        # видео/картинки чистим как сегодня (см. §5)
    else:
        tiktok_url = await upload_to_tiktok(video["video_path"], _tiktok_caption(video))
        ... # unchanged from today
```

The `if video["category"] == "catapult": block_reason = "..."` deterministic branch is removed — catapult now always calls `check_tiktok_compliance` like every other category.

### 2. New Claude call — `generate_tiktok_safe_script` (subagents/yt_script.py)

```python
async def generate_tiktok_safe_script(original_narration: str, category: str, block_reason: str) -> dict | None:
    """Переписывает narration в нейтральную, информационную подачу той же темы —
    без промо-формулировок, конкретных продуктов и призывов к действию — специально
    под TikTok. Возвращает {"narration": ..., "caption": ...}, или None при сбое."""
```

Same calling convention as `generate_video_script`/`check_tiktok_compliance` (direct `httpx` POST to `api.anthropic.com`). Prompt includes the original narration, the category, and the *specific* `block_reason` string from `check_tiktok_compliance` — telling Claude exactly what to remove/soften rather than guessing. Asks for a rewritten narration (same core facts/topic, neutral third-person informational framing) plus a short caption suitable for the TikTok post text.

### 3. `_attempt_tiktok_fallback` (subagents/yt_publisher.py) — orchestration, one attempt only

```python
async def _attempt_tiktok_fallback(video_id: str, video: dict, block_reason: str) -> tuple[str, str] | None:
    """Одна попытка более лояльной версии под TikTok. Возвращает (tiktok_url, note)
    при успехе, None если не получилось (сбой генерации/рендера, или повторный блок) —
    без повторных попыток."""
    fallback = await generate_tiktok_safe_script(video["narration"], video["category"], block_reason)
    if not fallback:
        return None

    if video.get("image_paths"):
        # скриптовые видео: новая озвучка + повторный рендер с теми же картинками
        audio_path = await generate_voiceover(fallback["narration"], f"{video_id}_tt")
        if not audio_path:
            return None
        video_path = await render_video(fallback["narration"], video["image_paths"], audio_path, f"{video_id}_tt")
        if not video_path:
            return None
    else:
        # самозапись: тот же файл, только новый caption — переозвучки/рендера нет,
        # т.к. в самозаписи и так нет наших субтитров/озвучки поверх
        video_path = video["video_path"]

    recheck = await check_tiktok_compliance(fallback["caption"])
    if recheck:
        return None  # тоже заблокировано — сдаёмся, без третьей попытки

    tiktok_url = await upload_to_tiktok(video_path, fallback["caption"])
    if not tiktok_url:
        return None
    return tiktok_url, "переозвучено под TikTok" if video.get("image_paths") else "новый текст под TikTok"
```

### 4. Data model changes (`send_video_for_approval` and its callers, `pending_videos`/`approved_videos`)

`send_video_for_approval` gains two new parameters, stored on the video dict alongside the existing `video_path`/`title`/`description`/`tags`/`category`/`thumbnail_path`:
- `narration`: source text for `generate_tiktok_safe_script` to rewrite. `generate_daily_short` (`orchestrator.py`) passes `script_data["narration"]` (already has it in scope). For self-record, `handle_video_file` (`subagents/yt_publisher.py`) currently discards `state["script"]` (the text the admin reads on camera) after building `title`/`description` via `generate_video_metadata` — it needs to start passing that script text through as `narration` too, so the fallback has real source material to work from even though self-record has no generated narration audio.
- `image_paths`: full list of image file paths used for the render (today only `thumbnail_path = image_paths[0]` is kept; `generate_daily_short` already has the full `image_paths` list in scope). Files aren't deleted after render today, so they stay available on the persistent `/data` volume for however long the video sits in the (now weekly) approval/publish queue. Not passed for self-record — this (not `narration`) is what `_attempt_tiktok_fallback` branches on to decide caption-only vs. full re-render.

### 5. Cleanup

If the fallback path renders a new video file (`{video_id}_tt.mp4`), it's removed after a successful TikTok upload, same as the primary video file is today. If the fallback fails at any step, no extra cleanup needed beyond what already happens for a plain block today.

## Error handling

- `generate_tiktok_safe_script` failure (Claude API error, malformed response) → `_attempt_tiktok_fallback` returns `None`, exactly like today's plain "TikTok пропущен" path — no fail-closed complexity needed here since a `None` here just means "no fallback happened," it doesn't affect YouTube or the fail-closed guarantee already established for `check_tiktok_compliance` itself.
- Fallback voiceover/render failure → same, `None`, give up.
- Fallback caption re-blocked by `check_tiktok_compliance` → give up, **do not** recurse into a third attempt. One fallback is the deliberate limit — repeatedly trying to out-guess the compliance checker would waste generation cost for diminishing returns.
- `tiktok_retry_pending` is unaffected — it still exists only for genuine upload-mechanism failures (network/Buffer API errors), not compliance blocks or fallback exhaustion, matching the existing convention from the original compliance-check design.

## Out of scope for this pass

- No fallback for a fallback (single retry only, per above).
- No change to `check_tiktok_compliance` itself or its prompt — reused as-is for both the original and fallback caption checks.
- No UI/admin control to disable the fallback for a specific video — it always attempts once when the original is blocked.
- Self-record's caption-only fallback doesn't get a fresh voiceover/render even though a future version could theoretically re-record narration over the admin's raw footage — out of scope; self-record videos have no pipeline-generated audio track to replace.
