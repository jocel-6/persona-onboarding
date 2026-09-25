"""Automated voice-call test: dials the agent over WebRTC and talks with synthesized speech.

    .venv/bin/python scripts/call_test.py            # backend must be running on :8000

Scenario: happy path -> "mhm" while the agent talks (should NOT interrupt) ->
"wait, actually..." while it talks (SHOULD interrupt) -> silence (check-in) -> hangup.
Prints a timeline and saves the agent's audio to data/call_test.wav.
"""

from __future__ import annotations

import asyncio
import fractions
import json
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import Settings  # noqa: E402

API = __import__("os").environ.get("API_URL", "http://localhost:8000")
SR = 48000
FRAME = 960  # 20 ms
USER_VOICE = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"  # a different Cartesia voice plays the user
OUT = Path(__file__).resolve().parent.parent / "data"

T0 = time.perf_counter()


def log(what: str) -> None:
    print(f"{time.perf_counter() - T0:6.1f}s  {what}", flush=True)


class Mic(MediaStreamTrack):
    """A fake microphone: silence, plus whatever speech we queue."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self.buf = bytearray()
        self.pts = 0
        self.start: float | None = None

    def say(self, pcm: bytes) -> float:
        self.buf += pcm
        return len(pcm) / 2 / SR  # seconds of speech queued

    async def recv(self) -> AudioFrame:
        if self.start is None:
            self.start = time.perf_counter()
        self.pts += FRAME
        delay = self.start + self.pts / SR - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        n = FRAME * 2
        chunk = bytes(self.buf[:n])
        del self.buf[:n]
        if len(chunk) < n:
            chunk += b"\0" * (n - len(chunk))
        frame = AudioFrame(format="s16", layout="mono", samples=FRAME)
        frame.planes[0].update(chunk)
        frame.sample_rate = SR
        frame.pts = self.pts
        frame.time_base = fractions.Fraction(1, SR)
        return frame


def tts(s: Settings, text: str) -> bytes:
    r = httpx.post(
        "https://api.cartesia.ai/tts/bytes",
        headers={"Cartesia-Version": "2026-03-01", "X-API-Key": s.cartesia_api_key},
        json={
            "model_id": "sonic-3.6",
            "transcript": text,
            "voice": {"mode": "id", "id": USER_VOICE},
            "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SR},
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.content


class Call:
    def __init__(self):
        self.events: list[tuple[float, str, dict]] = []
        self.bot_speaking = False
        self.agent_text = ""
        self.turns_done = 0
        self.recorded: list[np.ndarray] = []

    def on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        t, data = msg.get("type"), msg.get("data") or {}
        now = time.perf_counter() - T0
        self.events.append((now, t, data))
        if t == "bot-started-speaking":
            self.bot_speaking = True
            log("   [agent starts speaking]")
        elif t == "bot-stopped-speaking":
            self.bot_speaking = False
            log("   [agent stops speaking]")
        elif t == "user-started-speaking":
            log("   [you start speaking]")
        elif t == "user-transcription" and data.get("final"):
            log(f"   heard you: {data.get('text')!r}")
        elif t == "server-message":
            kind = data.get("type")
            if kind == "delta":
                self.agent_text += data.get("text", "")
            elif kind == "done":
                log(f"AGENT: {data.get('reply')}   (llm first token {data.get('latency', {}).get('ttft_ms')}ms)")
                self.agent_text = ""
            elif kind == "ui":
                log(f"   ui: {data.get('ui')}")
            elif kind == "state":
                self.turns_done += 1
                st = data.get("state", {})
                log(f"   state: user={st.get('user_name')!r} help={st.get('help_topic')!r} call={st.get('call_status')}")


async def wait_for(pred, timeout: float, what: str) -> bool:
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    log(f"!! timed out waiting for {what}")
    return False


async def main() -> None:
    s = Settings()
    async with httpx.AsyncClient(base_url=API, timeout=120) as http:
        sid = (await http.post("/api/sessions")).json()["state"]["session_id"]
        await http.post(f"/api/sessions/{sid}/messages", json={"text": "Kai"})
        await http.post(f"/api/sessions/{sid}/events", json={"type": "call_accepted"})
        log(f"session {sid[:8]}: named the agent Kai, phone is ringing, answering...")

        lines = {
            "intro": "Hi! I'm Maya. Honestly, my inbox has been a total disaster since school started.",
            "mhm": "Mhm.",
            "wait": "Wait, actually, hold on a second.",
            "followup": "Sorry, I just meant, can you also help with my calendar?",
        }
        audio = {k: tts(s, v) for k, v in lines.items()}

        pc = RTCPeerConnection()
        mic = Mic()
        pc.addTrack(mic)
        call = Call()
        dc = pc.createDataChannel("chat", ordered=True)

        @dc.on("open")
        def _open():
            dc.send(json.dumps({"label": "rtvi-ai", "type": "client-ready", "id": "c1",
                                "data": {"version": "1.0.0", "about": {"library": "call_test"}}}))

        dc.on("message", call.on_message)

        @pc.on("track")
        def _track(track):
            async def record():
                while True:
                    try:
                        frame = await track.recv()
                    except Exception:
                        return
                    arr = frame.to_ndarray()
                    call.recorded.append(arr.reshape(-1)[:: max(1, arr.shape[0])] if arr.ndim > 1 else arr)
            asyncio.ensure_future(record())

        await pc.setLocalDescription(await pc.createOffer())
        while pc.iceGatheringState != "complete":
            await asyncio.sleep(0.05)
        r = await http.post("/api/offer", json={
            "sdp": pc.localDescription.sdp, "type": pc.localDescription.type,
            "requestData": {"session_id": sid},
        })
        r.raise_for_status()
        ans = r.json()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))
        log("connected")

        # 1. Opener
        await wait_for(lambda: call.turns_done >= 1 and not call.bot_speaking, 25, "the opener")
        await asyncio.sleep(0.6)

        # 2. Happy path
        log("YOU: " + lines["intro"])
        mic.say(audio["intro"])
        n = call.turns_done
        await wait_for(lambda: call.bot_speaking, 20, "a reply to start")

        # 3. Backchannel while the agent talks: it should keep talking
        await asyncio.sleep(0.8)
        if call.bot_speaking:
            log("YOU (while it talks): Mhm.")
            mic.say(audio["mhm"])
            await asyncio.sleep(1.5)
            log(f"   -> still speaking after 'mhm'? {call.bot_speaking}")
        await wait_for(lambda: call.turns_done > n and not call.bot_speaking, 30, "the reply to finish")
        await asyncio.sleep(0.6)

        # 4. Real interruption: it should stop
        log("YOU: " + lines["followup"])
        mic.say(audio["followup"])
        await wait_for(lambda: call.bot_speaking, 20, "a reply to start")
        await asyncio.sleep(1.0)
        if call.bot_speaking:
            t_int = time.perf_counter()
            log("YOU (cutting in): " + lines["wait"])
            mic.say(audio["wait"])
            await wait_for(lambda: not call.bot_speaking, 5, "the agent to stop")
            log(f"   -> agent stopped {time.perf_counter() - t_int:.2f}s after you started talking")
            n = call.turns_done
            await wait_for(lambda: call.turns_done > n + 1, 30, "a reply to the interruption")  # trim + reply
        await wait_for(lambda: not call.bot_speaking, 30, "things to settle")
        await asyncio.sleep(1.0)

        # 4b. Interrupt again the way real people do ("No. No. No."), then expect a reply.
        #     This once deadlocked the call: it went silent after an interruption.
        if "nono" not in audio:
            audio["nono"] = tts(s, "No. No. No. No. That's not what I meant.")
            audio["ask"] = tts(s, "Can you tell me a bit about how you'd help with my calendar?")
        log("YOU: Can you tell me a bit about how you'd help with my calendar?")
        mic.say(audio["ask"])
        await wait_for(lambda: call.bot_speaking, 20, "a reply to start")
        await asyncio.sleep(1.0)
        n = call.turns_done
        log("YOU (cutting in): No. No. No. No. That's not what I meant.")
        mic.say(audio["nono"])
        replied = await wait_for(lambda: call.turns_done >= n + 2, 20, "a reply after 'No. No. No.'")
        log(f"   -> replied after repeated interruption? {replied}")
        await wait_for(lambda: not call.bot_speaking, 30, "things to settle")
        await asyncio.sleep(1.0)

        # 5. Silence: expect a gentle check-in
        n = call.turns_done
        log(f"(staying silent for {s.silence_checkin_secs + 4:.0f}s)")
        await wait_for(lambda: call.turns_done > n, s.silence_checkin_secs + 12, "a silence check-in")
        await wait_for(lambda: not call.bot_speaking, 15, "the check-in to finish")

        # 6. Hang up; the text follow-up should arrive over HTTP
        await pc.close()
        r = await http.post(f"/api/sessions/{sid}/events", json={"type": "hangup"})
        follow = "".join(json.loads(l[6:]).get("text", "") for l in r.text.splitlines()
                         if l.startswith("data: ") and '"delta"' in l)
        log(f"hung up. Text follow-up: {follow!r}")

    if call.recorded:
        pcm = np.concatenate(call.recorded).astype(np.int16)
        OUT.mkdir(exist_ok=True)
        with wave.open(str(OUT / "call_test.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        log(f"saved agent audio to {OUT / 'call_test.wav'}")


if __name__ == "__main__":
    asyncio.run(main())
