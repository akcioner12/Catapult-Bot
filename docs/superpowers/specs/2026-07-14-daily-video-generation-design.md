# Daily video generation + publish-now override — design

## Context

`generate_weekly_batch()` (`orchestrator.py`, cron Sunday 19:10) generates all 14 of the week's videos at once, each pre-assigned a `planned_day`/`planned_time` from `WEEKLY_SCHEDULE`. Approving a video (`vapprove`) no longer publishes it — it queues into `approved_videos`, and a per-slot cron (`publish_due_slot`) publishes it days later at its assigned time.

The topic-selection prompt (`generate_video_script`, `subagents/yt_script.py`) explicitly instructs Claude to pick "the single most resonant, hot story" from recent candidates (Telegram posts, YouTube trends, trending coins). Combined with the up-to-6-day gap between generation and publish, this means a genuinely timely topic (e.g. a same-day interest-rate announcement) can be selected Sunday night and not publish until the following Thursday — stale and potentially misleading to subscribers by then.

Considered and rejected: rewording the prompt to force evergreen-only topics. Rejected because it permanently forecloses timely content, when the real fix is structural — video should be generated close enough to publish time that "hot" and "timely" aren't in tension, the same way the text/photo pipeline already works (`evening_generation`, nightly, for next-day publish).

## Change 1 — daily generation instead of a weekly batch

Remove the Sunday-only cron (`parser.py` — `scheduler.add_job(generate_weekly_batch, "cron", day_of_week="sun", hour=19, minute=10)`). `generate_weekly_batch()` itself (`orchestrator.py`) and `/generate_video` (`cmd_generate_video`, `parser.py`) are unaffected — they remain available as an explicit manual "regenerate the whole week" action, just no longer fire automatically.

New function `generate_tomorrows_videos()` (`orchestrator.py`, next to `generate_weekly_batch`):

```python
async def generate_tomorrows_videos():
    tomorrow = datetime.now(KYIV_TZ) + timedelta(days=1)
    day_key = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][tomorrow.weekday()]
    entries = [e for e in WEEKLY_SCHEDULE if e["day"] == day_key]
    for entry in entries:
        planned_time = f'{entry["hour"]:02d}:{entry["minute"]:02d}'
        await _generate_and_queue_video(entry["category"], entry["day"], planned_time)
        await asyncio.sleep(2)
```

`KYIV_TZ` and `timedelta` aren't currently imported in `orchestrator.py` — add `from datetime import datetime, date, timedelta` (extending the existing `datetime, date` import) and `from subagents.yt_publisher import ..., KYIV_TZ` (extending the existing import list, which already pulls `WEEKLY_SCHEDULE` from the same module).

Called from the end of `evening_generation()` (`orchestrator.py`), directly after the existing "✅ Все посты подготовлены и одобрены автоматически!" admin notification — so video approval requests arrive in the same nightly batch, right after the text posts, matching how the user already reviews content once each evening for the next day. WEEKLY_SCHEDULE's Mon/Wed/Fri and Tue/Thu/Sat/Sun category pairings are unchanged — this only changes *when* each day's 1-2 videos get generated (the evening before, instead of the batch a week before).

## Change 2 — "Publish now" button

`video_approval_keyboard` (`subagents/yt_publisher.py`) gains a second button. The existing "✅ Одобрить и опубликовать" label is renamed to "✅ В очередь" — it hasn't actually published immediately since the queue/scheduled-publish redesign, and the old label is actively misleading now that a second, *actually* immediate button sits next to it:

```python
def video_approval_keyboard(video_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ В очередь", "callback_data": f"vapprove_{video_id}"},
            {"text": "🚀 Опубликовать сейчас", "callback_data": f"vpublishnow_{video_id}"},
        ], [
            {"text": "✏️ Изменить название", "callback_data": f"vedit_{video_id}"},
            {"text": "❌ Отменить", "callback_data": f"vcancel_{video_id}"},
        ]]
    }
```

`handle_video_approval`'s callback pattern (`parser.py`: `CallbackQueryHandler(handle_video_approval, pattern="^(vapprove|vcancel|vedit)_")`) extends to `"^(vapprove|vcancel|vedit|vpublishnow)_"`. New branch in `handle_video_approval` (`subagents/yt_publisher.py`), mirroring `publish_due_slot`'s existing YouTube-upload-then-`_finish_publish` shape:

```python
elif action == "vpublishnow":
    pending_videos.pop(video_id, None)
    save_pending_videos()
    await _edit_status(query, "🚀 Публикую немедленно...")
    youtube_id = await upload_to_youtube(video["video_path"], video["title"], video["description"], video["tags"])
    if youtube_id:
        await _finish_publish(video_id, video, youtube_id)
    else:
        failed_uploads[video_id] = video
        save_failed_uploads()
        await notify_admin("❌ Загрузка на YouTube не удалась при немедленной публикации. Попробуй /retry_videos позже.")
    if video.get("planned_day") and video.get("planned_time"):
        from orchestrator import _generate_and_queue_video
        asyncio.create_task(_generate_and_queue_video(video["category"], video["planned_day"], video["planned_time"]))
```

The `orchestrator` import is local to the function (not module-level) — `orchestrator.py` already imports from `subagents.yt_publisher` (`send_video_for_approval`, `WEEKLY_SCHEDULE`, etc.), so a module-level import in the other direction would be circular. The regeneration call is deliberately fire-and-forget (`asyncio.create_task`, not awaited) — video generation (script → voice → images → render) takes real time, and the admin's "published!" confirmation shouldn't wait on it. The backfill video goes through the normal `send_video_for_approval` flow at the end of `_generate_and_queue_video` — it isn't auto-queued, the admin still approves it like any other video, keeping one consistent review step for everything that ends up published.

If `planned_day`/`planned_time` are empty (self-record videos via `handle_video_file`, which calls `lookup_schedule_slot` but can return `("", "")` if the category isn't found — see existing code), no backfill is triggered — there's no weekly slot to keep from going empty.

## Error handling

- `generate_tomorrows_videos()` inherits `_generate_and_queue_video`'s existing per-step failure handling (logs a warning and returns on script/voice/image/render/metadata failure) — a failed entry just means no video for that slot tomorrow, same as today's per-video failure behavior in `generate_weekly_batch`.
- The "publish now" YouTube-upload failure path mirrors `publish_due_slot`'s existing failure path exactly (`failed_uploads` + `/retry_videos`), for consistency — no new failure-handling concept introduced.
- The backfill task's own failures (if `_generate_and_queue_video` fails) are silent by design — same as every other automatic generation failure elsewhere in this codebase (logged, not surfaced as an admin alert); the admin will simply notice a gap in tomorrow's approval messages, same as they would for any other generation failure.

## Out of scope

- The topic-selection prompt in `generate_video_script` is unchanged — daily generation closes the staleness gap structurally, so the "pick the hottest story" instruction is no longer in tension with publish timing.
- `/generate_video` (manual full-week regeneration) and `/generate_video_test` (single test video) are unaffected — both remain available as manual overrides.
- No change to `publish_due_slot` or the scheduled per-slot publish crons — videos that aren't published early via "🚀 Опубликовать сейчас" still publish at their normal planned time.
