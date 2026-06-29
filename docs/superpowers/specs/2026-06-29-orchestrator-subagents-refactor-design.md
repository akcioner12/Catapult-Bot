# Orchestrator + Subagents Refactor for the Content Pipeline (catapult-bot)

## Context

`catapult-bot` (GitHub: `akcioner12/catapult-bot`) runs three Railway services from one repo:

- `web` — `server.py` (FastAPI backend: Catapult Trade proxy, user/referral storage, dialog state, webhooks, Mini App data).
- `worker` — `bot.py` (`@catapulttrade_guide_bot`: affiliate onboarding — warmup dialog, quiz, Catapult Connect account linking, support chat).
- `parser` — `parser.py` (~2080 lines): **the live content pipeline** — monitors Telegram channels, scores posts by virality, rewrites via Claude, sends drafts for admin approval (with edit/rewrite/photo), and auto-publishes on a fixed daily schedule to `@Crypto_AI_Forex`. `parser.py` also runs a second bot instance (`@catapulttrade_guide_bot`, same `BOT_TOKEN` as `bot.py`) to handle the onboarding dialog and the actual channel publish — meaning `bot.py`'s `worker` service is very likely redundant with what `parser.py` already does internally (out of scope to fix here; noted for awareness only).

This pipeline is the original "AI SMM agent" the user is building (see `dirigent-agent`, an earlier from-scratch rebuild of the same idea that never reached production — superseded; see project memory `project_smm_agent_architecture`). `parser.py` is real, live, and currently posts to the user's actual channel on a 6-slot daily schedule. **It must keep working without interruption.**

The user wants `parser.py`'s content pipeline to become "the Dirigent" — a main orchestrator agent that will command a team of per-platform subagents, starting with Telegram (already working) and expanding next to Instagram, then Twitter, YouTube, TikTok (per the original brief's phased rollout).

## Goal

Restructure the content-pipeline portion of `parser.py` into an `orchestrator.py` + `subagents/` module layout, with **zero behavior change** — a pure code-organization refactor, not a rewrite. This creates the foundation for adding new platform subagents in later, separate work.

## Non-goals

- No changes to `bot.py` or the affiliate-onboarding/quiz/Catapult Connect code path (in `parser.py` or `bot.py`).
- No new platforms (Instagram/Twitter/YouTube/TikTok) in this pass — that's future work, brainstormed separately once this refactor is confirmed stable in production.
- No fix for the `bot.py`/`parser.py` token-overlap redundancy noted above — flagged for the user's awareness, not addressed here.
- No behavior or prompt changes to monitoring, scoring, rewriting, scheduling, or publishing logic.

## Module breakdown

All extracted code is moved verbatim (same logic, same prompts, same constants) from `parser.py` into:

- **`subagents/tg_monitor.py`** — `get_posts_tgstat`, `get_posts_web`, `parse_post_date`, `parse_post_views`, `viral_score`, `collect_top_posts`, `make_hash`, the `sent_hashes` set, `CHANNELS`, `TOP_POSTS`, `TGSTAT_TOKEN`/`TGSTAT_API_URL`.
- **`subagents/rewriter.py`** — `generate_post_claude`, `generate_catapult_post`, `STYLE_GUIDE`, `CATAPULT_ANGLES`, Claude API config (`CLAUDE_API_KEY`, `CLAUDE_API_URL`).
- **`subagents/image_brief.py`** — `generate_image_brief`.
- **`subagents/tg_publisher.py`** — `send_for_approval`, `approval_keyboard`, `handle_approval`, `handle_photo`, `auto_publish`, `CHANNEL_SIGNATURE`, `PHOTOS_DIR`, photo/pending/approved state helpers (`save_pending`, `save_approved`, `load_pending`, `PENDING_FILE`, `APPROVED_FILE`). Also a new `handle_admin_edit(update, context)` — the admin-only body currently inlined inside `handle_edit_message` (post-id lookup, text replacement, confirmation message) — extracted verbatim, no logic change.
  - `handle_edit_message` itself is a dispatcher (admin vs. non-admin branch) and stays in **`parser.py`**, since the non-admin branch belongs to the untouched onboarding flow. Its admin branch becomes a one-line call to `tg_publisher.handle_admin_edit(...)`. This is the one spot where a function is split rather than moved whole — flagged here so the diff review knows it's intentional and still behavior-preserving.
- **`subagents/weekly_plan.py`** — `generate_weekly_plan`, `send_weekly_plan`.
- **`orchestrator.py`** (the Dirigent) — `evening_generation` (the per-category cycle that calls monitor → rewrite → image-brief → send-for-approval), the poll-topic rotation state (`poll_idx`, `last_poll_date`, `POLL_TOPICS`), `catapult_angle_idx`, and the scheduler job definitions (`PUBLISH_SCHEDULE`, the cron wiring currently at the bottom of `main()`).
- **`parser.py`** (slimmed entrypoint) — bot app construction for both Telegram bots, handler registration (commands, callback patterns, message filters), `main()`. Imports everything else from `orchestrator` and `subagents/*`. The non-SMM handlers (`cmd_start`, quiz, Catapult Connect, etc.) stay exactly where they are today (still defined in `parser.py`, since they currently live there too, duplicated from `bot.py`) — not part of this refactor's scope.

## Data flow

Unchanged. `evening_generation` (cron, 20:00 Europe/Kiev) still calls `collect_top_posts` per category → `generate_post_claude`/`generate_catapult_post` → `generate_image_brief` → `send_for_approval` (writes to `pending_posts`, persisted to `PENDING_FILE`). Admin approval via buttons still moves posts into `approved_queue` (persisted to `APPROVED_FILE`). The existing per-slot cron jobs still call `auto_publish(slot)` to post to `CHANNEL_ID` (`@Crypto_AI_Forex`) via `MAIN_BOT_TOKEN`. Sunday 19:00 cron still calls `send_weekly_plan`.

## Safety & rollout plan

1. All work happens on a new branch (e.g. `refactor/orchestrator-subagents`) in the local clone — `main` is untouched until the user explicitly approves a merge.
2. Extraction is mechanical: each moved function is diffed against the original to confirm byte-for-byte logic parity (only imports and module-level wiring change).
3. After the refactor, `parser.py` must still register the exact same handlers, the exact same cron schedule, and produce an identical startup log.
4. The full diff is shown to the user for review before anything is pushed or merged.
5. Deploy to Railway only happens after the user's explicit go-ahead — and ideally not in the middle of a pending approval/publish cycle.

## Testing approach

Since this is Windows-side work against a cloned repo (not a live Railway shell), verification is by:
- Static review: function-by-function diff between old `parser.py` and new module locations.
- Local import sanity check (`python -c "import orchestrator"` etc.) to catch wiring/import errors before deploy — requires the same dependencies as `requirements.txt`; secrets are not needed for import-level checks but are needed to actually run the bot.
- The user reviewing the branch diff and, ideally, testing on Railway via a separate environment/branch deploy before merging to the production branch, given Railway is already connected to this repo.

## Future work (explicitly out of scope here)

Once this lands and the user confirms posting continues to work normally, the next phase (separate brainstorm + plan) is adding Instagram subagents (image generation, IG publisher) per the original brief's phased rollout, plumbed into the same `orchestrator.py`.
