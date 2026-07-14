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
