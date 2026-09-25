"""Short voice samples for the voice picker: "Hi, I'm Wren!" in each candidate voice.

Rendered once per (voice, name) with Cartesia's HTTP API and cached in memory, so a
sample costs a fraction of a cent the first time and nothing after.
"""

from __future__ import annotations

import io
import wave

import httpx

from ..config import Settings

CARTESIA_VERSION = "2026-03-01"  # matches pipecat's CartesiaTTSService
SAMPLE_RATE = 24000
_cache: dict[tuple[str, str], bytes] = {}


def _wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


async def sample(settings: Settings, voice_id: str, agent_name: str) -> bytes:
    key = (voice_id, agent_name)
    if key in _cache:
        return _cache[key]
    line = f"Hi, I'm {agent_name}! Want to hear what I can do?"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            "https://api.cartesia.ai/tts/bytes",
            headers={"Cartesia-Version": CARTESIA_VERSION, "X-API-Key": settings.cartesia_api_key},
            json={
                "model_id": settings.tts_model or "sonic-3.6",
                "transcript": line,
                "voice": {"mode": "id", "id": voice_id},
                "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
            },
        )
        r.raise_for_status()
    audio = _wav(r.content)
    if len(_cache) > 200:
        _cache.clear()
    _cache[key] = audio
    return audio
