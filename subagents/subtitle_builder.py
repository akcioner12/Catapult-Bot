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
Style: Default,DejaVu Sans,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,80,{margin_r},150,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# avatar_safe=True сужает правый край субтитров, чтобы текст не заезжал под
# кружок аватара (см. AVATAR_SIZE/AVATAR_MARGIN в yt_render.py).
AVATAR_SAFE_MARGIN_R = 460
AVATAR_SAFE_FONT_SIZE = 58
DEFAULT_MARGIN_R = 80
DEFAULT_FONT_SIZE = 64


def _format_ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def build_ass_subtitles(script_text: str, audio_duration: float, output_path: str, avatar_safe: bool = False) -> None:
    phrases = [p.strip() for p in re.split(r"(?<=[.!?])\s+", script_text.strip()) if p.strip()]

    margin_r = AVATAR_SAFE_MARGIN_R if avatar_safe else DEFAULT_MARGIN_R
    font_size = AVATAR_SAFE_FONT_SIZE if avatar_safe else DEFAULT_FONT_SIZE
    lines = [ASS_HEADER.format(font_size=font_size, margin_r=margin_r)]
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
