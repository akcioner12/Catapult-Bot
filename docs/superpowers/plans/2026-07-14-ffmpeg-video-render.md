# FFmpeg Self-Rendered Video Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace JSON2Video with a local FFmpeg render in `subagents/yt_render.py`, so the video pipeline never needs a paid third-party rendering API again.

**Architecture:** `render_video()` keeps its exact signature and `str | None` contract. Internally it now: probes the real audio duration (`ffprobe`), builds a phrase-timed `.ass` subtitle file from the already-known script text (no forced alignment — approximate, proportional-to-word-count timing), and runs one `ffmpeg` subprocess that applies a `zoompan` Ken Burns effect per image, concatenates them, burns the subtitles, and muxes in the audio.

**Tech Stack:** Python 3.11 (`asyncio.create_subprocess_exec`), `ffmpeg`/`ffprobe` CLI (installed on Railway via `RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg`, installed locally via winget for dev testing), DejaVu Sans Bold (bundled font, Cyrillic support).

## Global Constraints

- `render_video(script_text: str, image_paths: list[str], audio_path: str, filename: str) -> str | None` — signature and return contract must not change; no caller in `orchestrator.py` or `subagents/yt_publisher.py` should need edits.
- No forced alignment (aeneas/whisperx/etc.) — phrase-level timing proportional to word count is the confirmed, sufficient accuracy bar.
- This repo's Railway builder is **Railpack**, not Nixpacks — system packages go through `RAILPACK_DEPLOY_APT_PACKAGES`, not a `nixpacks.toml` file.
- No pytest/tests directory exists in this repo today — follow the existing convention (see `docs/superpowers/plans/2026-07-11-video-pipeline-overhaul.md`) of ad-hoc runnable verification scripts, not a new test framework.
- Never invoke real ElevenLabs/Gemini/JSON2Video-cost pipeline steps as a "test" — verify the render with synthetic `ffmpeg`-generated (lavfi) audio/images, which cost nothing.

---

### Task 1: Local ffmpeg + Railway runtime package

**Files:**
- None created/modified in this repo (this task is local-machine setup + a Railway service variable).

**Interfaces:**
- Produces: a working local `ffmpeg`/`ffprobe` on the dev machine (needed by Task 3's real render test) and `ffmpeg`/`ffprobe` present at runtime on the `Catapult-Bot` Railway service (needed by Task 4).

- [x] **Step 1: Check for local ffmpeg**

Run: `ffmpeg -version`
Expected: either a version banner (skip to Step 3), or `command not found` (continue to Step 2).

- [x] **Step 2: Install ffmpeg locally (Windows dev machine)**

Run: `winget install --id Gyan.FFmpeg -e`

After install, open a new shell (PATH needs to refresh) and re-run `ffmpeg -version` and `ffprobe -version` — both must print a version banner before continuing to Task 3.

- [x] **Step 3: Set the Railway runtime apt package for Catapult-Bot**

Run (from anywhere, using the project/service/environment IDs below — confirm they still match `railway status` first, since these can change):

```bash
railway variables --set "RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg" --skip-deploys \
  --project 2ffa99f8-afbd-4b52-88cd-23a31a9cb39d \
  --environment 63f574de-9e17-46bd-a5f4-e17f0a576f6f \
  --service 82216bd1-88e4-4581-b1d1-dcc60dc6340d
```

`--skip-deploys` is required — this project has a known incident (see project memory) where `variable set` without it triggered an unwanted redeploy from `main` instead of the branch under test. The variable will be picked up on the next explicit deploy (Task 4).

- [x] **Step 4: No commit for this task** (no repo files changed).

---

### Task 2: Subtitle builder (`subagents/subtitle_builder.py`)

**Files:**
- Create: `subagents/subtitle_builder.py`

**Interfaces:**
- Produces: `build_ass_subtitles(script_text: str, audio_duration: float, output_path: str) -> None` — writes an `.ass` subtitle file to `output_path`. Consumed by Task 3.

- [x] **Step 1: Write the verification script and run it (expect failure — module doesn't exist yet)**

Run this via a heredoc so nothing extra is committed to the repo:

```bash
python3 - <<'EOF'
from subagents.subtitle_builder import build_ass_subtitles
import tempfile, os

script = "Биткоин снова растёт. Аналитики удивлены таким скачком. Что будет дальше, узнаем скоро."
with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "test.ass")
    build_ass_subtitles(script, 9.0, path)
    content = open(path, encoding="utf-8").read()

    assert content.count("Dialogue:") == 3, f"expected 3 cues, got: {content}"
    assert "PlayResX: 1080" in content and "PlayResY: 1920" in content
    assert "0:00:00.00" in content, content
    # last cue's end time should be close to the full audio duration
    last_end = content.strip().splitlines()[-1].split(",")[2]
    h, m, s = last_end.split(":")
    end_seconds = int(h) * 3600 + int(m) * 60 + float(s)
    assert 8.9 <= end_seconds <= 9.0, f"last cue ends at {end_seconds}, expected ~9.0"
print("OK")
EOF
```

Expected: `ModuleNotFoundError: No module named 'subagents.subtitle_builder'`

- [x] **Step 2: Implement `subagents/subtitle_builder.py`**

```python
"""
Строит .ass-субтитры для рендера: делит текст сценария на фразы по знакам
препинания, тайминг каждой фразы — пропорционально числу слов от реальной
длительности озвучки (без forced alignment — точность "по фразам" достаточна).
"""
import re

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,64,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,80,80,150,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _format_ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def build_ass_subtitles(script_text: str, audio_duration: float, output_path: str) -> None:
    phrases = [p.strip() for p in re.split(r"(?<=[.!?])\s+", script_text.strip()) if p.strip()]

    lines = [ASS_HEADER]
    if phrases and audio_duration > 0:
        total_words = sum(len(p.split()) for p in phrases) or 1
        t = 0.0
        for phrase in phrases:
            duration = audio_duration * (len(phrase.split()) / total_words)
            start, end = t, t + duration
            text = phrase.replace("\n", " ")
            lines.append(
                f"Dialogue: 0,{_format_ass_timestamp(start)},{_format_ass_timestamp(end)},Default,,0,0,0,,{text}\n"
            )
            t = end

    with open(output_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
```

- [x] **Step 3: Re-run the verification script from Step 1**

Expected: `OK` printed, no assertion errors.

- [x] **Step 4: Commit**

```bash
git add subagents/subtitle_builder.py
git commit -m "feat: add phrase-timed .ass subtitle builder for local video render"
```

---

### Task 3: Bundle the subtitle font

**Files:**
- Create: `assets/fonts/DejaVuSans-Bold.ttf`

**Interfaces:**
- Produces: a Cyrillic-capable font file at a fixed repo-relative path, consumed by Task 4's `ffmpeg` `subtitles` filter (`fontsdir`).

- [x] **Step 1: Download DejaVu Sans Bold**

DejaVu Fonts is the standard permissively-licensed (public-domain-derived, redistribution explicitly allowed) font family with full Cyrillic coverage. Download the official release archive and extract only the one file needed:

```bash
mkdir -p assets/fonts
curl -L -o /tmp/dejavu.zip https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip
unzip -p /tmp/dejavu.zip "dejavu-fonts-ttf-2.37/ttf/DejaVuSans-Bold.ttf" > assets/fonts/DejaVuSans-Bold.ttf
rm /tmp/dejavu.zip
```

- [x] **Step 2: Verify the file**

Run: `file assets/fonts/DejaVuSans-Bold.ttf`
Expected: output mentions `TrueType` (confirms it's a real font file, not an HTML error page from a bad download).

- [x] **Step 3: Commit**

```bash
git add assets/fonts/DejaVuSans-Bold.ttf
git commit -m "feat: bundle DejaVu Sans Bold for burned-in subtitle rendering"
```

---

### Task 4: Rewrite `render_video` to use FFmpeg

**Files:**
- Modify: `subagents/yt_render.py` (full rewrite of the render internals; keep the module's public function name/signature)
- Modify: `subagents/media_push.py:1-4` (docstring only — no longer mentions JSON2Video as the reason photos/audio get pushed to `web`)

**Interfaces:**
- Consumes: `build_ass_subtitles(script_text, audio_duration, output_path) -> None` (Task 2).
- Produces: `render_video(script_text: str, image_paths: list[str], audio_path: str, filename: str) -> str | None` — unchanged signature/contract, consumed by `orchestrator.py:414` and `subagents/yt_publisher.py:414` (no changes needed there).

- [x] **Step 1: Write the verification script and run it (expect failure — old JSON2Video code has no local-render behavior to satisfy this)**

```bash
python3 - <<'EOF'
import asyncio, os, subprocess, tempfile
from subagents.yt_render import render_video

async def main():
    with tempfile.TemporaryDirectory() as d:
        audio_path = os.path.join(d, "audio.mp3")
        img1 = os.path.join(d, "img1.png")
        img2 = os.path.join(d, "img2.png")

        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=6", audio_path], check=True, capture_output=True)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1080x1920:d=1", "-frames:v", "1", img1], check=True, capture_output=True)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=1080x1920:d=1", "-frames:v", "1", img2], check=True, capture_output=True)

        result = await render_video("Тест. Рендер видео работает.", [img1, img2], audio_path, "smoketest")
        assert result is not None, "render_video returned None"
        assert os.path.exists(result), f"{result} does not exist"

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", result],
            capture_output=True, text=True, check=True,
        )
        out_duration = float(probe.stdout.strip())
        assert 5.5 <= out_duration <= 6.5, f"output duration {out_duration}, expected ~6s"

        os.remove(result)
        os.remove(result.replace(".mp4", ".ass"))
    print("OK")

asyncio.run(main())
EOF
```

Expected: fails (old implementation tries to call the JSON2Video API and either errors on a missing/invalid key or returns `None`).

- [x] **Step 2: Rewrite `subagents/yt_render.py`**

```python
"""
Sub-agent: сборка вертикального видео (картинки + озвучка + вшитые субтитры)
локальным ffmpeg — без стороннего рендер-API. Ken Burns через zoompan,
субтитры — приблизительный, пофразный тайминг (см. subtitle_builder.py),
без forced alignment.
"""
import asyncio
import logging
import os

from subagents.subtitle_builder import build_ass_subtitles

logger = logging.getLogger(__name__)

VIDEOS_DIR = "/data/videos"
os.makedirs(VIDEOS_DIR, exist_ok=True)

FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")

RENDER_TIMEOUT_SECONDS = 300
FPS = 25


def _escape_ffmpeg_path(path: str) -> str:
    """ffmpeg filter option values treat ':' as a separator — Windows paths
    (C:\\...) need it escaped; a no-op on Railway's Linux paths."""
    return path.replace("\\", "/").replace(":", "\\:")


async def _ffprobe_duration(path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {stderr.decode()}")
    return float(stdout.decode().strip())


def _build_ffmpeg_command(image_paths: list[str], audio_path: str, ass_path: str, audio_duration: float, output_path: str) -> list[str]:
    duration_per_image = audio_duration / len(image_paths)
    frames = max(int(duration_per_image * FPS), 1)

    cmd = ["ffmpeg", "-y"]
    for path in image_paths:
        cmd += ["-loop", "1", "-t", f"{duration_per_image:.3f}", "-i", path]
    cmd += ["-i", audio_path]

    filter_chains = []
    labels = []
    for i in range(len(image_paths)):
        # чередуем направление панорамирования — рецепт из документации ffmpeg
        # zoompan: старт на первом кадре (on==1), затем инкремент/декремент x за кадром
        if i % 2 == 0:
            x_expr = "if(eq(on,1),0,x+1)"
        else:
            x_expr = "if(eq(on,1),(iw-iw/zoom),x-1)"
        label = f"v{i}"
        filter_chains.append(
            f"[{i}:v]scale=2160:3840:flags=lanczos,"
            f"zoompan=z='min(zoom+0.0015,1.2)':d={frames}:"
            f"x='{x_expr}':y='ih/2-(ih/zoom/2)':s=1080x1920:fps={FPS},setsar=1[{label}]"
        )
        labels.append(f"[{label}]")

    concat_inputs = "".join(labels)
    filter_chains.append(f"{concat_inputs}concat=n={len(image_paths)}:v=1:a=0[vconcat]")
    filter_chains.append(
        f"[vconcat]subtitles='{_escape_ffmpeg_path(ass_path)}':fontsdir='{_escape_ffmpeg_path(FONTS_DIR)}'[vout]"
    )

    audio_index = len(image_paths)
    cmd += [
        "-filter_complex", ";".join(filter_chains),
        "-map", "[vout]",
        "-map", f"{audio_index}:a",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        output_path,
    ]
    return cmd


async def render_video(script_text: str, image_paths: list[str], audio_path: str, filename: str) -> str | None:
    """Рендерит вертикальное видео 1080x1920 локальным ffmpeg. None при сбое/таймауте."""
    if not image_paths or not audio_path:
        logger.warning("render_video: нет картинок или озвучки — пропускаем")
        return None

    try:
        audio_duration = await _ffprobe_duration(audio_path)
        ass_path = f"{VIDEOS_DIR}/{filename}.ass"
        build_ass_subtitles(script_text, audio_duration, ass_path)

        output_path = f"{VIDEOS_DIR}/{filename}.mp4"
        cmd = _build_ffmpeg_command(image_paths, audio_path, ass_path, audio_duration, output_path)

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=RENDER_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            logger.error(f"ffmpeg render failed: {stderr.decode()[-2000:]}")
            return None

        logger.info(f"✅ Видео отрендерено: {output_path}")
        return output_path
    except asyncio.TimeoutError:
        logger.error(f"render_video: рендер не завершился за {RENDER_TIMEOUT_SECONDS}с")
        return None
    except Exception as e:
        logger.error(f"render_video error: {e}")
        return None
```

- [x] **Step 3: Update the stale docstring in `subagents/media_push.py`**

The current docstring says files are pushed to `web` so it can serve them to JSON2Video — that's no longer true for rendering (only TikTok/Buffer and self-record uploads still need it). Replace:

```python
"""
Отправляет сгенерированные медиа-файлы на web (у Catapult-Bot и web разные volume
на Railway, /data не общий — см. Task 14 диагностику), чтобы web мог раздать их
JSON2Video через /media.
"""
```

with:

```python
"""
Отправляет сгенерированные медиа-файлы на web (у Catapult-Bot и web разные volume
на Railway, /data не общий — см. Task 14 диагностику), чтобы web мог раздать их
по HTTP для TikTok/Buffer и self-record загрузок. Рендер видео (yt_render.py)
теперь читает эти файлы локально и в этой раздаче не нуждается.
"""
```

- [x] **Step 4: Re-run the verification script from Step 1**

Expected: `OK` printed — confirms a real synthetic render (blue image → red image, tone audio, burned-in subtitles) succeeds end to end using only free, local `ffmpeg`-generated inputs.

- [x] **Step 5: Commit**

```bash
git add subagents/yt_render.py subagents/media_push.py
git commit -m "feat: render video locally with ffmpeg instead of JSON2Video"
```

---

### Task 5: Deploy and verify on Railway

**Files:** none (deployment + remote verification only).

**Interfaces:** none new — this task verifies Task 1/4's work actually functions in the real Railway environment.

- [x] **Step 1: One-off deploy of this branch to `Catapult-Bot`**

From the branch checkout (not `main` — this mirrors the project's established pattern of testing a branch on Railway via `railway up` without merging, so `main`/production traffic source is untouched):

```bash
railway up --service 82216bd1-88e4-4581-b1d1-dcc60dc6340d \
  --project 2ffa99f8-afbd-4b52-88cd-23a31a9cb39d \
  --environment 63f574de-9e17-46bd-a5f4-e17f0a576f6f
```

Expected: build succeeds, deploy status SUCCESS.

- [x] **Step 2: Confirm ffmpeg is present in the deployed container**

```bash
railway ssh -s Catapult-Bot -- ffmpeg -version
```

Expected: a version banner (confirms `RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg` from Task 1 actually took effect on this deploy).

- [x] **Step 3: Run the same synthetic render remotely**

```bash
railway ssh -s Catapult-Bot -- bash -c "ffmpeg -y -f lavfi -i 'sine=frequency=440:duration=6' /tmp/audio.mp3 && ffmpeg -y -f lavfi -i 'color=c=blue:s=1080x1920:d=1' -frames:v 1 /tmp/img1.png && ffmpeg -y -f lavfi -i 'color=c=red:s=1080x1920:d=1' -frames:v 1 /tmp/img2.png"
```

```bash
railway ssh -s Catapult-Bot -- python3 -c "import asyncio; from subagents.yt_render import render_video; print('RESULT:', asyncio.run(render_video('Тест. Рендер на Railway работает.', ['/tmp/img1.png','/tmp/img2.png'], '/tmp/audio.mp3', 'railway_smoketest')))"
```

Expected: `RESULT: /data/videos/railway_smoketest.mp4` (not `None`).

- [x] **Step 4: Clean up the smoke-test artifacts**

```bash
railway ssh -s Catapult-Bot -- rm -f /tmp/audio.mp3 /tmp/img1.png /tmp/img2.png /data/videos/railway_smoketest.mp4 /data/videos/railway_smoketest.ass
```

- [x] **Step 5: No commit for this task** (deployment/verification only — the branch is not merged to `main` here; that decision happens separately once the user has reviewed everything, same as the original YouTube Shorts pipeline branch was handled).
