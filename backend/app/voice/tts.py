"""The voice layer: switching text-to-speech provider or voice is config, not code.

    TTS_PROVIDER=cartesia | elevenlabs | openai
    TTS_VOICE_ID=...        (required for cartesia and elevenlabs)
    TTS_MODEL=...           (optional; provider default otherwise)
"""

from __future__ import annotations

from pipecat.services.tts_service import TextAggregationMode, TTSService

from ..config import Settings


def make_tts(settings: Settings, *, provider: str | None = None, voice_id: str | None = None) -> TTSService:
    provider = provider or settings.tts_provider
    voice = voice_id or settings.tts_voice_id or None
    model = settings.tts_model or None

    if provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService as Service

        key = settings.cartesia_api_key
    elif provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService as Service

        key, model = settings.elevenlabs_api_key, model or "eleven_flash_v2_5"
    elif provider == "openai":
        from pipecat.services.openai.tts import OpenAITTSService as Service

        key, voice = settings.openai_api_key, voice or "alloy"
    else:
        raise ValueError(f"unknown TTS provider {provider!r}")

    opts = {k: v for k, v in {"voice": voice, "model": model}.items() if v}
    mode = settings.tts_text_mode or ("token" if provider == "cartesia" else "sentence")
    return Service(
        api_key=key,
        settings=Service.Settings(**opts),
        text_aggregation_mode=TextAggregationMode.TOKEN if mode == "token" else TextAggregationMode.SENTENCE,
    )
