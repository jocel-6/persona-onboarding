"""#15 Voice evals: real audio through the real call pipeline, scored for what text evals can't see.

    .venv/bin/python scripts/voice_eval.py            # prints the plan and cost estimate
    .venv/bin/python scripts/voice_eval.py --yes      # runs it (backend on API_URL, default :8000)

Each scenario is scripted (known words), spoken by a different synthetic voice, sometimes
fast or with a mid-sentence pause, over WebRTC exactly like a browser. Scored on:
  * word error rate: what Deepgram heard vs. what was said
  * key terms: names, times and numbers that must come through right
  * outcome: did the agent save the right name?
  * speed: end of speech to the agent's first sound (median / worst), per turn

Cost per full run: a few cents of Claude (short Haiku/Sonnet turns), plus Deepgram and
Cartesia cents. Results: evals/results/voice-<stamp>.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.call_test as ct  # noqa: E402  (reuses the proven fake mic + event recorder)
from app.config import Settings  # noqa: E402

API = ct.API
RESULTS = Path(__file__).resolve().parent.parent / "evals" / "results"
VOICES = {
    "Skylar": "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4",
    "Cathy": "e8e5fffb-252c-436d-b842-8879b84445b6",
    "Parker": "30894953-bcce-41fe-892c-15ce19c843ff",
}


@dataclass
class Line:
    text: str
    pause_after: float = 0.0  # seconds of silence before the next part of the SAME turn (tests merging)
    continues: bool = False  # True: the next Line is the rest of this thought


@dataclass
class Scenario:
    id: str
    voice: str
    lines: list[Line]
    expect_name: str | None = None
    key_terms: list[list[str]] = field(default_factory=list)  # each inner list: acceptable spellings
    speed: float = 1.0


SCENARIOS = [
    Scenario("baseline", "Skylar", [Line("Hi, I'm Maya, and honestly my inbox has been a total disaster since school started.")],
             expect_name="Maya", key_terms=[["maya"], ["inbox"]]),
    Scenario("unusual_name", "Cathy", [Line("Hey, it's Siobhan. I keep double booking myself between work and my kids' soccer.")],
             expect_name="Siobhan", key_terms=[["siobhan", "shivon", "chevonne"], ["soccer"]]),
    Scenario("times_numbers", "Parker", [
        Line("I'm Dev. Can you add a dentist appointment on Thursday at two thirty PM?"),
    ], expect_name="Dev", key_terms=[["2:30", "two thirty", "230"], ["thursday"], ["dentist"]]),
    Scenario("fast_talker", "Skylar", [Line("I'm Priya, I'm swamped, just help me keep my meetings straight please.")],
             expect_name="Priya", key_terms=[["priya"], ["meetings"]], speed=1.3),
    Scenario("pause_mid_thought", "Cathy", [
        Line("I'm Jordan and what I really need is,", pause_after=1.4, continues=True),
        Line("help remembering my friends' birthdays."),
    ], expect_name="Jordan", key_terms=[["jordan"], ["birthdays"]]),
    Scenario("correction", "Parker", [
        Line("Hi, I'm Maya."),
        Line("Sorry, actually it's Mia. I need help with my calendar."),
    ], expect_name="Mia", key_terms=[["mia"], ["calendar"]]),
]


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower().replace("’", "'"))


def wer(truth: str, heard: str) -> float:
    a, b = words(truth), words(heard)
    d = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(b) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (a[i - 1] != b[j - 1]))
            prev = cur
    return d[len(b)] / max(1, len(a))


def tts(s: Settings, text: str, voice: str, speed: float) -> bytes:
    body = {
        "model_id": "sonic-3.6",
        "transcript": text,
        "voice": {"mode": "id", "id": VOICES[voice]},
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": ct.SR},
    }
    if speed != 1.0:
        body["generation_config"] = {"speed": speed}
    r = httpx.post("https://api.cartesia.ai/tts/bytes", json=body, timeout=30,
                   headers={"Cartesia-Version": "2026-03-01", "X-API-Key": s.cartesia_api_key})
    r.raise_for_status()
    return r.content


async def run(sc: Scenario, s: Settings) -> dict:
    ct.log = lambda *_: None  # quiet the recorder
    async with httpx.AsyncClient(base_url=API, timeout=120) as http:
        sid = (await http.post("/api/sessions")).json()["state"]["session_id"]
        await http.post(f"/api/sessions/{sid}/messages", json={"text": "Kai"})
        await http.post(f"/api/sessions/{sid}/events", json={"type": "call_accepted"})

        pc, mic, call = RTCPeerConnection(), ct.Mic(), ct.Call()
        pc.addTrack(mic)
        dc = pc.createDataChannel("chat", ordered=True)
        dc.on("open", lambda: dc.send(json.dumps({"label": "rtvi-ai", "type": "client-ready", "id": "c1",
                                                  "data": {"version": "1.0.0", "about": {"library": "voice_eval"}}})))
        dc.on("message", call.on_message)
        await pc.setLocalDescription(await pc.createOffer())
        while pc.iceGatheringState != "complete":
            await asyncio.sleep(0.05)
        ans = (await http.post("/api/offer", json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type,
                                                   "requestData": {"session_id": sid}})).json()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))
        await ct.wait_for(lambda: call.turns_done >= 1 and not call.bot_speaking, 30, "opener")
        await asyncio.sleep(0.6)

        latencies = []
        start_idx = len(call.events)
        for i, line in enumerate(sc.lines):
            mic.say(tts(s, line.text, sc.voice, sc.speed))
            while mic.buf:
                await asyncio.sleep(0.02)
            if line.continues:
                await asyncio.sleep(line.pause_after)
                continue
            t_end = time.perf_counter() - ct.T0
            n = call.turns_done
            got = await ct.wait_for(
                lambda: any(t == "bot-started-speaking" and ts > t_end for ts, t, _ in call.events), 20, "a reply")
            if got:
                first = min(ts for ts, t, _ in call.events if t == "bot-started-speaking" and ts > t_end)
                latencies.append(round((first - t_end) * 1000))
            await ct.wait_for(lambda: call.turns_done > n and not call.bot_speaking, 30, "reply to finish")
            await asyncio.sleep(0.6)

        heard = " ".join(d.get("text", "") for _, t, d in call.events[start_idx:]
                         if t == "user-transcription" and d.get("final"))
        await pc.close()
        await http.post(f"/api/sessions/{sid}/events", json={"type": "hangup"})
        state = (await http.get(f"/api/sessions/{sid}")).json()["state"]
        await http.delete(f"/api/sessions/{sid}")

    truth = " ".join(l.text for l in sc.lines)
    heard_l = heard.lower()
    terms = [any(opt in heard_l for opt in alts) for alts in sc.key_terms]
    return {
        "scenario": sc.id, "voice": sc.voice, "speed": sc.speed,
        "wer": round(wer(truth, heard), 3), "key_terms": f"{sum(terms)}/{len(terms)}",
        "name_saved": (state.get("user_name") or "").lower() == (sc.expect_name or "").lower(),
        "saved_name": state.get("user_name"), "heard": heard, "latency_ms": latencies,
    }


def report(rows: list[dict]) -> str:
    lat = [v for r in rows for v in r["latency_ms"]]
    lines = [
        "# Voice eval", "",
        f"_{time.strftime('%Y-%m-%d %H:%M')}: {len(rows)} scripted calls over real audio._", "",
        f"**Word error rate (mean): {statistics.mean(r['wer'] for r in rows):.1%}** · "
        f"**names saved correctly: {sum(r['name_saved'] for r in rows)}/{len(rows)}** · "
        f"**end of speech → first sound: median {statistics.median(lat) if lat else 0:.0f} ms, "
        f"worst {max(lat) if lat else 0} ms**", "",
        "| Scenario | Voice | WER | Key terms | Name saved | Response (ms) | Heard |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['scenario']} | {r['voice']}{' ×' + str(r['speed']) if r['speed'] != 1 else ''} | {r['wer']:.0%} | "
            f"{r['key_terms']} | {'✅' if r['name_saved'] else '❌ ' + str(r['saved_name'])} | "
            f"{', '.join(map(str, r['latency_ms'])) or '–'} | {r['heard'][:90]} |"
        )
    return "\n".join(lines)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="Actually run (spends a few cents).")
    ap.add_argument("--only", default="", help="Comma-separated scenario ids.")
    args = ap.parse_args()
    chosen = [sc for sc in SCENARIOS if not args.only or sc.id in args.only.split(",")]
    print(f"{len(chosen)} scripted calls against {API}: " + ", ".join(sc.id for sc in chosen))
    print("Estimated cost: ~$0.02-0.03 per call (Claude + Deepgram + Cartesia).")
    if not args.yes:
        print("Dry run. Re-run with --yes to spend it.")
        return
    s = Settings()
    rows = []
    for sc in chosen:
        print(f"  running {sc.id}...", flush=True)
        rows.append(await run(sc, s))
    md = report(rows)
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"voice-{stamp}.md").write_text(md)
    (RESULTS / f"voice-{stamp}.json").write_text(json.dumps(rows, indent=2))
    print("\n" + md)


if __name__ == "__main__":
    asyncio.run(main())
