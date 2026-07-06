"""
Sub-agent: озвучка сценария через ElevenLabs TTS.
"""
import os
import logging

import httpx

logger = logging.getLogger(__name__)

ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
AUDIO_DIR = "/data/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

async def generate_voiceover(script_text: str, filename: str) -> str | None:
    """Генерирует mp3-озвучку через ElevenLabs. Возвращает путь к файлу или None."""
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        logger.warning("ELEVENLABS_API_KEY/ELEVENLABS_VOICE_ID не заданы — пропускаем озвучку")
        return None
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "text": script_text,
                    "model_id": "eleven_multilingual_v2",
                },
            )
            if resp.status_code != 200:
                logger.error(f"ElevenLabs API error {resp.status_code}: {resp.text[:300]}")
                return None

            local_path = f"{AUDIO_DIR}/{filename}.mp3"
            with open(local_path, "wb") as f:
                f.write(resp.content)

            logger.info(f"✅ Озвучка сгенерирована: {local_path}")
            return local_path
    except Exception as e:
        logger.error(f"generate_voiceover error: {e}")
        return None
