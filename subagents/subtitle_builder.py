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
