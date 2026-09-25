"""#5: hear tone of voice, not just words (Hume Expression Measurement, prosody model).

The user's last utterance is analyzed in parallel with the reply, so it never adds
latency; what it hears shapes the *next* turn ("their voice sounded tense, tired").
Off unless HUME_API_KEY is set. Any failure is silent: tone is a bonus, never a blocker.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import wave

log = logging.getLogger("persona.hume")

HUME_WS = "wss://api.hume.ai/v0/stream/models"

# Hume emotion names -> plain words for the director's note, and our mood labels.
PLAIN = {
    "Anger": "irritated", "Annoyance": "annoyed", "Anxiety": "anxious", "Distress": "stressed",
    "Tiredness": "tired", "Boredom": "bored", "Confusion": "unsure", "Disappointment": "let down",
    "Sadness": "down", "Excitement": "excited", "Joy": "happy", "Amusement": "amused",
    "Interest": "curious", "Calmness": "calm", "Determination": "focused", "Contentment": "content",
}
FRUSTRATED = {"Anger", "Annoyance", "Distress", "Disappointment"}
ENTHUSIASTIC = {"Excitement", "Joy", "Amusement"}


def to_wav(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def summarize(emotions: dict[str, float]) -> tuple[str | None, str | None]:
    """(plain-words tone for the note, a mood label or None). Only confident, notable signals."""
    top = sorted(((k, v) for k, v in emotions.items() if k in PLAIN), key=lambda kv: -kv[1])[:3]
    top = [(k, v) for k, v in top if v >= 0.25]
    if not top:
        return None, None
    words = ", ".join(PLAIN[k] for k, _ in top)
    lead, score = top[0]
    mood = "frustrated" if lead in FRUSTRATED and score >= 0.4 else "enthusiastic" if lead in ENTHUSIASTIC and score >= 0.45 else None
    return words, mood


async def analyze(api_key: str, wav: bytes, timeout: float = 8.0) -> dict[str, float]:
    """Average prosody emotion scores over the utterance. {} on any failure."""
    try:
        from websockets.asyncio.client import connect

        async with connect(HUME_WS, additional_headers={"X-Hume-Api-Key": api_key}, open_timeout=5) as ws:
            await ws.send(json.dumps({"models": {"prosody": {}}, "data": base64.b64encode(wav).decode()}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout))
    except Exception as e:  # noqa: BLE001 - tone is optional; never break the call
        log.warning("hume analysis skipped: %s", type(e).__name__)
        return {}
    totals: dict[str, float] = {}
    preds = (resp.get("prosody") or {}).get("predictions") or []
    for p in preds:
        for e in p.get("emotions", []):
            totals[e["name"]] = totals.get(e["name"], 0.0) + float(e.get("score", 0))
    return {k: v / len(preds) for k, v in totals.items()} if preds else {}
