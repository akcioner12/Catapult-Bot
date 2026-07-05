# YouTube Shorts Auto-Posting Pipeline (catapult-bot)

## Context

The Telegram side of the SMM system (`parser.py` + `orchestrator.py` + `subagents/`) is live and working: it monitors competitor channels, rewrites news via Claude, generates images, and publishes to `@Crypto_AI_Forex` with admin approval in Telegram. See [[project_smm_agent_architecture]].

The user wants to expand to a second platform — YouTube Shorts — as the first step of a longer-term multi-platform plan (YouTube → TikTok → Instagram, all under the same brand). The explicit goal of the YouTube channel is **not** view count for its own sake — it's a funnel to grow the `@Crypto_AI_Forex` Telegram channel, by posting short (30–60s), vertical, algorithm-friendly clips on the same crypto/AI/forex/Catapult topics already covered in Telegram.

There is an existing, fully-configured `tg-youtube-bot` service in the same Railway project (working YouTube OAuth token, publishes to `@dukhovni_syly`). **Confirmed with the user: this is a completely unrelated project** — different niche, different channel, coincidentally hosted in the same Railway project. See [[project_tg_youtube_bot_unrelated]]. This spec builds an entirely separate pipeline; nothing from `tg-youtube-bot` is reused.

No YouTube channel or Google Cloud project exists yet for the Crypto/AI/Forex/Catapult brand — both need to be created from scratch (manual, user-driven, with guidance) before implementation can be tested end-to-end.

## Goal

Add a YouTube Shorts pipeline to the existing `Catapult-Bot` Railway service (same repo, same admin Telegram bot — not a new service) that:

1. Once a day, auto-generates one 30–60s vertical video from the same news pipeline already feeding Telegram (crypto/AI/forex/Catapult), and sends it to the admin in Telegram for approval before publishing to YouTube.
2. Periodically proposes a topic + a script for the user to record themselves (not AI-generated) — the user replies with a video file, which the bot processes (title/description/tags) and sends for the same approval step.
3. On admin approval, uploads the video to YouTube and posts an announcement with the video link into the `@Crypto_AI_Forex` Telegram channel.
4. Uses YouTube Shorts search (via the same YouTube Data API credentials) once as part of topic selection, to bias auto-generated topics toward what's currently resonating in the niche — inspiration only. The bot never downloads, reuses, or re-uploads another creator's footage or audio; only the topic/hook idea informs a freshly written script.

## Non-goals

- TikTok and Instagram — explicitly deferred to their own future brainstorm/spec once this YouTube pipeline is live and stable.
- Fully autonomous publishing — every video (auto-generated or self-recorded) requires admin approval in Telegram before it goes to YouTube, same pattern as the existing TG post queue. Revisit later once the pipeline has a track record, same as the standing autonomy follow-up in [[project_smm_agent_autonomy_followup]].
- Automating JSON2Video account creation/rotation. The user will manually manage the JSON2Video account (starting on its free 600-credit tier, moving to a paid plan — Hobby, $16.95/mo billed yearly — at their own discretion) and update the `JSON2VIDEO_API_KEY` Railway variable by hand when needed. The bot does not create accounts, rotate emails, or otherwise automate around JSON2Video's free-tier limits.
- Long-form YouTube content, channel art/branding design — out of scope; this spec is the Shorts posting pipeline only.

## Architecture

New subagent modules alongside the existing `subagents/` package, wired into `orchestrator.py`'s scheduler and `parser.py`'s handlers — no new Railway service, no new bot token.

- **`subagents/yt_ideas.py`** — queries the YouTube Data API (`search.list`) for trending/high-performing Shorts in the crypto/AI/forex niche (by keyword, sorted by view count within a recent window). Returns a short list of topic/hook strings for the script generator to use as inspiration. Read-only, no video/audio download.
- **`subagents/yt_script.py`** — `generate_video_script(post_text_or_topic, category)`: Claude call producing a spoken narration script (~30–60s of speech, roughly 90–150 words) plus 2–4 image-brief prompts (reusing `subagents/image_brief.py`'s style-per-category approach) timed to the narration. Also `generate_self_record_script(category)` for the periodic "record yourself" prompt — same style guide, written to be read aloud by the user rather than narrated by TTS.
- **`subagents/yt_voice.py`** — `generate_voiceover(script_text, filename) -> audio_path`: calls ElevenLabs TTS, saves an mp3 to the existing `/data` volume (mirrors `PHOTOS_DIR` pattern in `tg_publisher.py`). Returns `None` on failure (same graceful-degradation pattern as `generate_image` / `generate_post_claude`).
- **`subagents/yt_render.py`** — `render_video(script_text, image_paths, audio_path, filename) -> video_path`: builds a JSON2Video render request (image sequence with pan/zoom, burned-in captions synced to the narration, the generated voiceover, a royalty-free background track) and polls for the rendered mp4. Returns `None` on failure — including the specific case of the JSON2Video account being out of credits (`402`/quota-exceeded style response) or the whole render call failing.
- **`subagents/yt_publisher.py`** — mirrors `tg_publisher.py`'s approval-queue shape but for video: `pending_videos` / `approved_videos` dicts persisted to `/data/pending_videos.json` / `/data/approved_videos.json`; `send_video_for_approval(...)` (sends the rendered mp4 to the admin via `sendVideo` with an approve/edit-title/cancel keyboard); `upload_to_youtube(video_path, title, description, tags) -> youtube_video_id` (YouTube Data API `videos.insert`, resumable upload, category + `madeForKids: false`, privacy status defaults to `public` — the whole point is algorithmic distribution for the Telegram funnel; `unlisted` would defeat that. Configurable via env var if the user wants to hold videos private during initial setup/testing); `announce_in_telegram(youtube_video_id)` (posts the `youtu.be/...` link to `@Crypto_AI_Forex` via the existing `MAIN_BOT_TOKEN`/`CHANNEL_ID`, same channel the TG posts already go to).
- **`orchestrator.py`** additions — `generate_daily_short()`: pulls a topic from the same `collect_top_posts` categories used for TG (optionally cross-referencing `yt_ideas` for a hook angle), calls `yt_script` → `yt_voice` → image_brief/`generate_image` (existing, reused as-is) → `yt_render` → `yt_publisher.send_video_for_approval`. Scheduled once daily at 21:00 Europe/Kiev (after the existing 20:00 `evening_generation`, so it doesn't compete with it for API rate limits) — easy to move at implementation time. A second scheduled job, `propose_self_record_script()`, fires weekly (Sunday, alongside the existing weekly content-plan cron) and sends the user a topic + script to record, via a plain Telegram message (no video yet — that comes back later as a user-submitted video).
- **`parser.py`** additions — a `MessageHandler` for admin-submitted video files (parallel to the existing photo handler) that, when the admin is in a "waiting for self-recorded video" state, hands the file to `yt_publisher` for title/description generation and approval-queue insertion. New `CallbackQueryHandler` patterns for the video approval keyboard (`^(vapprove|vcancel|vedit)_`), following the same naming convention as the existing `q*`/`approve`/`cancel` patterns.

## Data flow

**Auto-generated path:** `collect_top_posts(category)` (existing) → `yt_script.generate_video_script` → `yt_voice.generate_voiceover` (ElevenLabs) → `generate_image_brief` + `generate_image` (existing, gpt-image-1) for 2–4 stills → `yt_render.render_video` (JSON2Video: images + voiceover + burned captions + royalty-free music → mp4) → `yt_publisher.send_video_for_approval` (Telegram, admin) → on approval → `yt_publisher.upload_to_youtube` → `yt_publisher.announce_in_telegram`.

**Self-recorded path:** `orchestrator.propose_self_record_script` (Telegram message to admin with topic + script) → admin records and sends a video file back → `parser.py`'s video-message handler → `yt_publisher` generates title/description via Claude from the script text → `send_video_for_approval` → same approval → upload → announce.

**Idea sourcing:** `yt_ideas.py` is called from `generate_daily_short()` as an optional input to script generation — never runs standalone, never triggers a publish, never stores or serves another creator's media.

## Error handling & cost boundaries

Every external call in the new path can fail independently and must degrade the same way the existing pipeline already does (see the `rewriter.py` fix earlier this session: return `None`/`""` and skip, never crash the scheduler):

- ElevenLabs failure or quota exhaustion → `generate_voiceover` returns `None` → `generate_daily_short` logs and skips that day's video (no partial/broken upload attempt).
- JSON2Video failure or out-of-credits → `render_video` returns `None` → same skip-and-log behavior. This is the expected way the user's manual free-tier-then-paid-tier management surfaces: when credits run out, the daily job simply stops producing videos (visible in logs / an admin notification) until the user updates `JSON2VIDEO_API_KEY`.
- YouTube upload failure (quota, auth expiry) → log and notify the admin in Telegram; the approved video stays in `approved_videos` for retry rather than being silently dropped.

No new fully-automated retry/rotation logic is introduced for any of these — consistent with the non-goal of not automating around JSON2Video's limits.

## Credentials / setup (manual, user-driven)

New Railway environment variables on the existing `Catapult-Bot` service (none of this touches `tg-youtube-bot`):

- `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` — from a new Google Cloud project + OAuth consent screen set up for the Crypto/AI/Forex/Catapult YouTube channel (to be created). Stored as three plain env vars rather than the single base64-pickled blob pattern `tg-youtube-bot` uses, for easier inspection/rotation.
- `ELEVENLABS_API_KEY` — new ElevenLabs account (Starter plan, $6/mo, sufficient for ~30 short scripts/month).
- `JSON2VIDEO_API_KEY` — new JSON2Video account, starting on the free 600-credit tier; user manages upgrades/rotation manually.

## Cost estimate (at 1 auto-video/day)

| Item | Monthly |
|---|---|
| Claude (script) | ~$0.21 |
| gpt-image-1 (3 images/video, `high`) | ~$22.50 (or ~$5.70 at `medium`) |
| ElevenLabs (Starter) | $6.00 |
| JSON2Video | $0 while on free credits; $16.95/mo (billed yearly) once the user opts into Hobby |
| **Total** | **~$28–46/mo**, separate from existing TG-side Claude/OpenAI usage |

## Testing approach

- No automated test suite exists for the bot (Telegram-bot-shaped code, verified live) — same as the rest of the project. Verification is manual: trigger `generate_daily_short()` via a new `/generate_video` admin command (mirroring `/generate`/`/test_generate`), inspect the Telegram preview, approve, confirm the YouTube upload and the Telegram channel announcement.
- JSON2Video and ElevenLabs calls are validated against their real APIs during implementation using the free tiers (JSON2Video's 600 free credits, ElevenLabs' free tier) before any paid plan is purchased.
- Exercise the failure paths deliberately once (e.g. temporarily invalid `JSON2VIDEO_API_KEY`) to confirm the pipeline skips and logs rather than crashing the scheduler — same bar as the `rewriter.py` empty-text fix earlier this session.

## Future work (explicitly out of scope here)

- TikTok and Instagram Reels publishing, once this pipeline is stable — likely reusing the same script/voice/render stages with a different publisher module per platform.
- Revisiting full autonomy (no admin approval step) once there's a track record, per [[project_smm_agent_autonomy_followup]].
- Possibly moving off JSON2Video to a different render approach if cost or reliability becomes an issue at higher video volume.
