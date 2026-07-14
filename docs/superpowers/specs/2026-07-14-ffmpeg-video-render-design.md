# FFmpeg self-rendered video — design

## Context

The YouTube Shorts pipeline renders vertical video (`subagents/yt_render.py`, `render_video()`) through JSON2Video: pan/zoom over the generated images, burned-in auto-transcribed subtitles, the ElevenLabs voiceover track. The weekly batch (2026-07-11 overhaul) now generates up to 14 videos in one run instead of 1/day, and the first real run (2026-07-12) burned through JSON2Video's entire free allotment (16 one-time credits) after only 4 videos. The user has paid to upgrade ElevenLabs (Starter, $6/mo) but explicitly does not want to pay for JSON2Video — Hobby ($16.95/mo) requires annual prepay upfront, and even Professional ($49.95/mo) is an ongoing cost for something FFmpeg can do for free using Railway compute that's already running.

Key realization from the current code: `image_paths`/`audio_path` passed into `render_video()` are already local files on the same `Catapult-Bot` container's disk (written by `generate_image`/`generate_voiceover`). The only reason they're currently pushed to the `web` service's public `/media` endpoint is that JSON2Video, as an external API, needs public HTTPS URLs. A local FFmpeg render doesn't need that round trip at all for this step.

Subtitle accuracy requirement (confirmed with user): phrase-level/approximate is fine — no need for real forced alignment (aeneas/whisperx and similar add heavy dependencies for a repo that has none of that infrastructure today). Per-phrase timing is estimated proportionally to word count against the real audio duration (via `ffprobe`), the same idea as today's `WORDS_PER_SECOND` heuristic but anchored to the actual rendered audio file instead of a guess.

## Goal

Replace `render_video()`'s JSON2Video implementation with a local FFmpeg-based one, **without changing its signature or contract**: `render_video(script_text: str, image_paths: list[str], audio_path: str, filename: str) -> str | None`, returning a local mp4 path on success and `None` on any failure — so `orchestrator.py` and `subagents/yt_publisher.py` need no changes.

## Architecture

### 1. `nixpacks.toml` (new, repo root) — install `ffmpeg` on Railway

Railway currently builds this repo with default Nixpacks and no extra system packages. Add:

```toml
[phases.setup]
nixPkgs = ["ffmpeg"]
```

Applies to both Railway services built from this repo (`Catapult-Bot` worker and `web`), though only `Catapult-Bot` actually renders.

### 2. `assets/fonts/DejaVuSans-Bold.ttf` (new, committed binary)

No font ships in the repo today. DejaVu Sans (permissive license, ~700KB, full Cyrillic coverage) is bundled so burned-in subtitles render correctly regardless of what fonts happen to exist in the Railway build image. Referenced by path from the FFmpeg `subtitles` filter's `fontsdir` option.

### 3. `subagents/subtitle_builder.py` (new module — pure function, no I/O side effects beyond writing the one file)

```python
def build_ass_subtitles(script_text: str, audio_duration: float, output_path: str) -> None:
    """Splits script_text into phrases on sentence-ending punctuation (. ! ?),
    assigns each phrase a time window proportional to its word count against
    audio_duration, and writes an .ass file with those cues styled for a
    1080x1920 canvas (bottom-anchored, DejaVu Sans Bold). Line wrapping within
    a phrase is left to libass's automatic word-wrap — no manual \\N insertion."""
```

Pure and independent of FFmpeg/subprocess — trivially unit-testable (given script text + a duration, assert phrase boundaries and cue timings).

### 4. `subagents/yt_render.py` — rewritten `render_video`

```python
async def render_video(script_text: str, image_paths: list[str], audio_path: str, filename: str) -> str | None:
    if not image_paths or not audio_path:
        logger.warning("render_video: нет картинок или озвучки — пропускаем")
        return None

    try:
        audio_duration = await _ffprobe_duration(audio_path)   # subprocess: ffprobe -show_entries format=duration
        ass_path = f"{VIDEOS_DIR}/{filename}.ass"
        build_ass_subtitles(script_text, audio_duration, ass_path)

        output_path = f"{VIDEOS_DIR}/{filename}.mp4"
        cmd = _build_ffmpeg_command(image_paths, audio_path, ass_path, audio_duration, output_path)

        proc = await asyncio.create_subprocess_exec(*cmd, stdout=..., stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            logger.error(f"ffmpeg render failed: {stderr.decode()[-2000:]}")
            return None
        return output_path
    except (asyncio.TimeoutError, Exception) as e:
        logger.error(f"render_video error: {e}")
        return None
```

`_build_ffmpeg_command` constructs the filter graph:
- per image: `-loop 1 -t {duration_per_image} -i {path}`, then `scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,zoompan=z='min(zoom+0.0015,1.2)':d=<frames>:s=1080x1920:fps=25` (zoom 1.0→1.2 over the image's duration, alternating pan left/right per image index — same visual as today's JSON2Video config: `zoom: 2`, alternating `pan`);
- `concat=n=<count>:v=1:a=0` to join the per-image segments in order;
- `subtitles={ass_path}:fontsdir=assets/fonts` burned onto the concatenated video;
- `-i {audio_path}` mapped as the output audio stream, `-shortest` so output duration matches audio.

`duration_per_image = audio_duration / len(image_paths)`, replacing today's `total_seconds` word-count guess — same role, now anchored to the real audio file.

No more "out of credits" branch (`402`/`429` handling) — that failure mode doesn't exist for a local render; the only failure path is "ffmpeg exited non-zero" or "timed out."

## Data flow

```
script_text + image_paths + audio_path
    -> ffprobe: audio_duration
    -> subtitle_builder: phrases + per-phrase timing -> .ass file
    -> ffmpeg: per-image zoompan -> concat -> burn subtitles -> mux audio
    -> mp4 at VIDEOS_DIR/filename.mp4
```

`media_push.py`'s push-to-`web` step is no longer needed *for rendering* (JSON2Video's public-URL requirement was the only reason for it here) — it stays in place for TikTok/Buffer upload and self-record uploads, which are unaffected by this change.

## Error handling

- Missing images/audio args → `None` immediately (unchanged from today).
- `ffprobe` failure (corrupt/missing audio file) → caught, logged, `None`.
- `ffmpeg` non-zero exit → log the tail of stderr, `None`.
- Render exceeding 5 minutes (`asyncio.wait_for` timeout) → treated as failure, `None`. Generous relative to the ~30-60s clips actually being produced; guards the Sunday 14-video batch against one stuck job blocking the rest indefinitely (mirrors the intent of today's ~10-minute JSON2Video poll bound).
- Contract unchanged: every caller already treats `None` as "skip this video, log a warning" — no caller-side changes needed.

## Testing

Unlike ElevenLabs/JSON2Video/Gemini calls, a local FFmpeg render has no per-call cost — it's pure compute. This means, uniquely for this piece of the pipeline, the implementation plan can include a **real** end-to-end render (real images + real audio in, inspect the actual mp4 out) rather than only mocking `render_video` in tests, per the project's existing rule against invoking paid pipeline steps as tests.

- Unit tests for `subtitle_builder.build_ass_subtitles`: phrase splitting on punctuation, per-phrase timing proportional to word count, total duration matches `audio_duration`.
- One real render using a short sample audio file and 2-3 sample images, run locally (needs `ffmpeg` installed on the dev machine — e.g. `choco install ffmpeg` on Windows) and on Railway after deploy, checking the resulting mp4 plays, has burned-in subtitles roughly in sync, and the Ken Burns effect is visible.
- `ffmpeg` non-zero exit and timeout paths can be tested by feeding deliberately invalid input (e.g. a corrupt audio file) and asserting `render_video` returns `None`.

## Out of scope for this pass

- Forced alignment / word-level subtitle sync — explicitly not needed (approximate phrase-level timing confirmed sufficient).
- Any change to `media_push.py`'s TikTok/self-record usage — only the render step's dependency on it is removed.
- Alternate third-party render APIs (Shotstack, Creatomate) — considered and rejected: their free/sandbox tiers don't cover the ~60 videos/month volume without a paid plan, which defeats the purpose.
- Smooth crossfade transitions between images (`xfade`) — today's JSON2Video config uses hard cuts between scenes; this pass keeps that, not introducing new visual behavior beyond matching Ken Burns.
