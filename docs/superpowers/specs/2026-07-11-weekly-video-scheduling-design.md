# Weekly batch generation + scheduled auto-publish for Shorts/TikTok — design

## Context

Today the pipeline generates one video per day (`generate_daily_short`, cron 21:00 Kyiv, categories `crypto/ai/forex/catapult` round-robin), sends it to the admin for approval, and publishes to YouTube + TikTok **immediately** when the admin taps "✅ Одобрить" (`handle_video_approval` → `upload_to_youtube` → `_finish_publish`). Self-record videos (`propose_self_record_script`, weekly Sunday 19:05 → manual upload via `/upload` → `process_self_record_uploads`) join the same `send_video_for_approval` → immediate-publish path.

The user wants to move to a weekly cadence: generate a full week's videos at once, review/edit/approve them all in one sitting, and have the bot publish each one automatically at a pre-planned day/time — no per-video manual publish action. Target volume: 14 videos/week (2/day), split **crypto 4, ai 4, forex 3, catapult 3**, at category-appropriate times of day. Decided through research + discussion (see conversation): forex only makes sense on weekdays (FX markets closed weekends); crypto/ai/catapult content performs best weekday evenings (~18:00–21:00) and weekend early afternoons (~12:00–14:00).

## Weekly schedule

| Day | Slot 1 | Slot 2 |
|---|---|---|
| Mon | forex — 08:30 | crypto — 19:00 |
| Tue | ai — 18:30 | catapult — 20:00 |
| Wed | forex — 08:30 | crypto — 19:00 |
| Thu | ai — 18:30 | catapult — 20:00 |
| Fri | forex — 08:30 | crypto — 19:00 |
| Sat | ai — 12:30 | catapult — 14:00 |
| Sun | crypto — 12:30 | ai — 14:00 |

Totals: forex 3, crypto 4, ai 4, catapult 3 = 14. All times Europe/Kiev (matches existing `AsyncIOScheduler(timezone="Europe/Kiev")`).

This table is a plain Python constant (`WEEKLY_SCHEDULE`, list of `{"day": "mon", "hour": 8, "minute": 30, "category": "forex"}` dicts, 14 entries) — not user-editable via UI in this pass, just a code constant the user can ask to have changed later.

## Architecture

### 1. Weekly batch generation (new)

New scheduler job replacing the daily `generate_daily_short` trigger:

```python
scheduler.add_job(generate_weekly_batch, "cron", day_of_week="sun", hour=19, minute=10)
```

(19:10 — right after the existing `send_weekly_plan` content-plan job at 19:00, same placement logic as today's `generate_daily_short`.)

`generate_weekly_batch()` in `orchestrator.py` iterates `WEEKLY_SCHEDULE` (14 entries) and, for each entry, runs the **same generation steps `generate_daily_short` runs today** (`collect_top_posts` → `get_trending_shorts_ideas` → `generate_video_script` → `generate_voiceover` → `generate_image` × N → `render_video` → `generate_video_metadata`) for that entry's `category`, then calls `send_video_for_approval(..., planned_day=entry["day"], planned_time=f'{entry["hour"]:02d}:{entry["minute"]:02d}')`.

If any single entry's generation fails partway (script/voice/image/render/metadata failure) — same as today: log a warning and skip that entry. The result is simply a missing item for that slot this week (see §4, "empty slot" handling). No retry-within-the-batch logic — matches the existing per-video failure handling, just repeated 14 times instead of once. `generate_daily_short` and its `short_category_idx` round-robin are removed (superseded by the explicit weekly table).

### 2. Data model changes

`subagents/yt_publisher.py`: each video dict (in `pending_videos` / `approved_videos`) gains two fields:

- `planned_day`: `"mon"`..`"sun"`
- `planned_time`: `"HH:MM"` string

Set once at generation time (§1), never modified afterward. `pending_videos.json` / `approved_videos.json` keep their existing shape otherwise — no new files, no DB.

### 3. Approval flow — decouple "approve" from "publish"

`handle_video_approval`'s `vapprove` branch changes from *upload immediately* to *mark queued*:

```python
if action == "vapprove":
    pending_videos.pop(video_id, None)
    approved_videos[video_id] = video          # video already has planned_day/planned_time
    save_pending_videos()
    save_approved_videos()
    await _edit_status(query, f"✅ В очереди. Выйдет: {DAY_NAMES_RU[video['planned_day']]}, {video['planned_time']}")
```

No `upload_to_youtube` / `_finish_publish` call here anymore. "✏️ Изменить название" and "❌ Отменить" (`vedit` / `vcancel`) are unchanged. Self-record videos flow through the identical `send_video_for_approval` → this same handler, so they're covered automatically — their `planned_day`/`planned_time` come from whatever `category` they were generated for, looked up against `WEEKLY_SCHEDULE` at generation time in `process_self_record_uploads`/`propose_self_record_script`'s caller the same way §1 does it.

`approved_videos` becomes the durable "queue" — no separate queue file needed, it already persists to `APPROVED_FILE` and survives restarts.

### 4. Scheduled publish (new) — 4 cron jobs replace immediate publish-on-approval

```python
scheduler.add_job(publish_due_slot, "cron", day_of_week="mon,wed,fri", hour=8,  minute=30, args=["forex"])
scheduler.add_job(publish_due_slot, "cron", day_of_week="mon,wed,fri", hour=19, minute=0,  args=["crypto"])
scheduler.add_job(publish_due_slot, "cron", day_of_week="tue,thu",     hour=18, minute=30, args=["ai"])
scheduler.add_job(publish_due_slot, "cron", day_of_week="tue,thu",     hour=20, minute=0,  args=["catapult"])
scheduler.add_job(publish_due_slot, "cron", day_of_week="sat",         hour=12, minute=30, args=["ai"])
scheduler.add_job(publish_due_slot, "cron", day_of_week="sat",         hour=14, minute=0,  args=["catapult"])
scheduler.add_job(publish_due_slot, "cron", day_of_week="sun",         hour=12, minute=30, args=["crypto"])
scheduler.add_job(publish_due_slot, "cron", day_of_week="sun",         hour=14, minute=0,  args=["ai"])
```

(8 jobs total — one per distinct (day, time, category) cell in the table; several share the same category+time across multiple weekdays via APScheduler's `day_of_week` list syntax, so this isn't 14 separate job registrations.)

```python
async def publish_due_slot(category: str):
    """Publishes the oldest still-unpublished approved video for `category`
    whose planned slot has arrived. Self-healing: if nothing was approved in
    time, skip and notify; the next occurrence of this slot type will pick up
    whatever is approved by then, in the order it was approved."""
    now = datetime.now(KYIV_TZ)
    candidates = [
        (vid, v) for vid, v in approved_videos.items()
        if v["category"] == category
    ]
    if not candidates:
        await notify_admin(f"⚠️ Не было одобренного {category}-видео к {now:%H:%M} — слот пропущен.")
        return

    video_id, video = candidates[0]   # dict preserves insertion order == approval order
    approved_videos.pop(video_id, None)
    save_approved_videos()

    youtube_id = await upload_to_youtube(video["video_path"], video["title"], video["description"], video["tags"])
    if youtube_id:
        await _finish_publish(video_id, video, youtube_id)
    else:
        approved_videos[video_id] = video   # put back — /retry_videos already handles this shape
        save_approved_videos()
        await notify_admin("❌ Загрузка на YouTube не удалась для запланированного видео. Сохранено — попробуй /retry_videos позже.")
```

Picking "oldest approved" rather than matching `planned_day`/`planned_time` exactly is the deliberate self-healing behavior discussed: if the admin approves late, or a slot's generation failed and the admin fills the gap by approving a differently-planned video for that category, the queue still drains sensibly instead of requiring exact-slot bookkeeping. `planned_day`/`planned_time` are shown to the admin at approval time for transparency but aren't consulted at publish time — this is deliberately looser than a rigid calendar so a missed/late approval self-heals at the next matching slot instead of needing manual intervention.

`_finish_publish` (YouTube announce + TikTok compliance check + TikTok upload + admin summary) is unchanged — still runs at actual-publish time, not at approval time, so a caption that was fine a week ago still gets a fresh TikTok compliance check right before it goes out. `/retry_videos` and `/retry_tiktok` are unchanged.

`notify_admin(text: str)` is a new small helper in `yt_publisher.py` that wraps the `httpx.AsyncClient(...).post(".../sendMessage", json={"chat_id": ADMIN_TG_ID, ...})` boilerplate already duplicated 3× in that file (`handle_video_approval`'s YouTube-failure branch, `_finish_publish`, `retry_tiktok_upload`) — pulling it out is a small, directly-motivated cleanup (those 3 call sites switch to it too), not scope creep.

### 5. Removed / superseded

- `generate_daily_short` and the daily 21:00 cron trigger for it — replaced by `generate_weekly_batch` (§1).
- `short_category_idx` round-robin counter — replaced by the explicit `WEEKLY_SCHEDULE` table.

## Error handling

- Empty queue at a scheduled publish time → skip + one-line admin notification (§4). No retry loop, no queue reshuffling — next occurrence of that (day-set, time, category) slot tries again.
- YouTube upload failure at publish time → video is put back into `approved_videos` (so `/retry_videos` still works against it) + admin notified. Matches today's failure handling, just deferred to the scheduled trigger instead of the approval click.
- TikTok compliance block / TikTok upload failure → unchanged (`_finish_publish` already handles both).
- Generation failure for one of the 14 weekly slots → that slot has no candidate when its publish time arrives → falls into the "empty queue" case above.

## Out of scope for this pass

- No admin-facing UI to edit `WEEKLY_SCHEDULE` itself (day/time/category-mix) — it's a code constant; changing the mix means asking for a code change, same as this session.
- No re-ordering/drag-and-drop of the queue — publish order within a category is strictly FIFO by approval time.
- No change to the Telegram-post pipeline (`PUBLISH_SCHEDULE` in `orchestrator.py`) — this design only covers YouTube Shorts / TikTok video publishing.
- No automatic A/B testing or performance-based re-optimization of the schedule — the user explicitly said they'll revisit the table manually if weekend numbers look off.
