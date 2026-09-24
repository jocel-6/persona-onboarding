"""Voice bake-off (tech design section 8): pick the agent's voice with data, not a guess.

    .venv/bin/python scripts/voices.py list                  # real voices for every provider you have a key for
    .venv/bin/python scripts/voices.py render cartesia:<id> elevenlabs:<id> openai:coral ...
                                                             # every voice reads the test script; measures latency
    open data/voice_bakeoff/listen.html                      # blind listening page for friends (round 2)
    .venv/bin/python scripts/voices.py score ratings/*.json  # merge ratings into the README scorecard

Then set TTS_PROVIDER / TTS_VOICE_ID in .env to the winner and do round 3: live calls.
"""

from __future__ import annotations

import io
import json
import random
import statistics
import sys
import time
import wave
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import Settings  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "voice_bakeoff"
CARTESIA_VERSION = "2026-03-01"  # matches pipecat's CartesiaTTSService
SAMPLE_RATE = 24000
LATENCY_RUNS = 3  # time-to-first-audio is the median of this many requests per voice

# Section 8.3: lines chosen to cover what the agent actually says.
TEST_SCRIPT = [
    ("greeting", "Hey, it's Nova! So, what's been eating up most of your week?"),
    ("question", "So what's been taking up most of your time lately?"),
    ("empathy", "Ugh, that sounds exhausting. Let's make this quick."),
    ("excitement", "Oh nice, I can already see a few of those. Want me to sort them?"),
    ("dates_times", "You've got the dentist Thursday at 9:15 a.m."),
    ("names", "Got it, Siobhan. And you want to call me Kai?"),
    ("long_sentence", "Once your inbox is connected, I'll pull out anything with a deadline, like permission slips, so nothing sneaks up on you."),
    ("short_filler", "Got it."),
]
OPENAI_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def _pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _request(s: Settings, provider: str, voice: str, text: str) -> tuple[str, str, dict, dict | None]:
    """(url, method, headers, json body) for a streaming synthesis request returning raw 16-bit PCM."""
    if provider == "cartesia":
        return (
            "https://api.cartesia.ai/tts/bytes",
            "POST",
            {"Cartesia-Version": CARTESIA_VERSION, "X-API-Key": s.cartesia_api_key},
            {
                "model_id": s.tts_model or "sonic-3.6",
                "transcript": text,
                "voice": {"mode": "id", "id": voice},
                "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
            },
        )
    if provider == "elevenlabs":
        return (
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream?output_format=pcm_{SAMPLE_RATE}",
            "POST",
            {"xi-api-key": s.elevenlabs_api_key},
            {"text": text, "model_id": s.tts_model or "eleven_flash_v2_5"},
        )
    if provider == "openai":
        return (
            "https://api.openai.com/v1/audio/speech",
            "POST",
            {"Authorization": f"Bearer {s.openai_api_key}"},
            {"model": "gpt-4o-mini-tts", "voice": voice, "input": text, "response_format": "pcm",
             "instructions": "Warm, casual, and quick, like a friendly, competent assistant. Never salesy."},
        )
    raise SystemExit(f"unknown provider {provider!r}")


def synthesize(client: httpx.Client, s: Settings, provider: str, voice: str, text: str) -> tuple[bytes, float]:
    """Return (wav bytes, milliseconds to first audio byte)."""
    url, method, headers, body = _request(s, provider, voice, text)
    t0 = time.perf_counter()
    first: float | None = None
    chunks: list[bytes] = []
    with client.stream(method, url, headers=headers, json=body) as r:
        if r.status_code != 200:
            raise RuntimeError(f"{provider} {r.status_code}: {r.read().decode(errors='replace')[:300]}")
        for chunk in r.iter_bytes():
            if chunk and first is None:
                first = time.perf_counter() - t0
            chunks.append(chunk)
    return _pcm_to_wav(b"".join(chunks)), (first or 0) * 1000


def list_voices(s: Settings) -> None:
    with httpx.Client(timeout=30) as c:
        if s.cartesia_api_key:
            r = c.get("https://api.cartesia.ai/voices", params={"limit": 100},
                      headers={"Cartesia-Version": CARTESIA_VERSION, "X-API-Key": s.cartesia_api_key})
            r.raise_for_status()
            data = r.json()
            voices = data.get("data", data) if isinstance(data, dict) else data
            print(f"\n== Cartesia ({len(voices)})")
            for v in voices:
                if str(v.get("language", "en")).startswith("en"):
                    print(f"  cartesia:{v['id']}  {v.get('name', '')}: {(v.get('description') or '')[:90]}")
        else:
            print("\n== Cartesia: set CARTESIA_API_KEY to list voices")
        if s.elevenlabs_api_key:
            r = c.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": s.elevenlabs_api_key})
            if r.status_code != 200:
                detail = r.json().get("detail", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
                print(f"\n== ElevenLabs: error {r.status_code}: {detail.get('message', r.text[:200])}")
                voices = None
            else:
                voices = r.json().get("voices", [])
        if s.elevenlabs_api_key and voices is not None:
            print(f"\n== ElevenLabs ({len(voices)})")
            for v in voices:
                labels = ", ".join(f"{k}={val}" for k, val in (v.get("labels") or {}).items())
                print(f"  elevenlabs:{v['voice_id']}  {v.get('name', '')}: {labels[:90]}")
        elif not s.elevenlabs_api_key:
            print("\n== ElevenLabs: set ELEVENLABS_API_KEY to list voices")
        print("\n== OpenAI (baseline)" + ("" if s.openai_api_key else ": set OPENAI_API_KEY to use"))
        print("  " + "  ".join(f"openai:{v}" for v in OPENAI_VOICES))
    print("\nPick 3-4 friendly, conversational voices per provider (not narrators), then run `render`.")


# ---------------------------------------------------------------------------
# Render + blind listening page
# ---------------------------------------------------------------------------


def render(s: Settings, specs: list[str]) -> None:
    if not specs:
        raise SystemExit("usage: voices.py render provider:voice_id [provider:voice_id ...]")
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    with httpx.Client(timeout=60) as c:
        for spec in specs:
            provider, _, voice = spec.partition(":")
            slug = f"{provider}_{voice}".replace("/", "_")
            (OUT / slug).mkdir(exist_ok=True)
            ttfbs = []
            try:
                for key, line in TEST_SCRIPT:
                    wav, ttfb = synthesize(c, s, provider, voice, line)
                    (OUT / slug / f"{key}.wav").write_bytes(wav)
                    ttfbs.append(ttfb)
                for _ in range(LATENCY_RUNS - 1):  # extra runs on the greeting for a steadier median
                    ttfbs.append(synthesize(c, s, provider, voice, TEST_SCRIPT[0][1])[1])
            except Exception as e:  # keep going: one bad voice shouldn't sink the bake-off
                print(f"  {spec}: FAILED {e}")
                continue
            med = statistics.median(ttfbs)
            results.append({"spec": spec, "slug": slug, "provider": provider, "voice": voice, "ttfb_ms": round(med)})
            print(f"  {spec}: time to first audio median {med:.0f}ms")
    if not results:
        raise SystemExit("\nNo voices rendered; see the errors above.")
    (OUT / "render.json").write_text(json.dumps(results, indent=2))
    _write_listen_page(results)
    print(f"\nWrote {OUT}/listen.html. Play it for 3-5 friends; each downloads a ratings file.")


def _write_listen_page(results: list[dict]) -> None:
    shuffled = results[:]
    random.shuffle(shuffled)
    blind = [{"label": f"Voice {chr(65 + i)}", "slug": r["slug"]} for i, r in enumerate(shuffled)]
    lines = [{"key": k, "text": t} for k, t in TEST_SCRIPT]
    html = LISTEN_HTML.replace("__VOICES__", json.dumps(blind)).replace("__LINES__", json.dumps(lines))
    (OUT / "listen.html").write_text(html)


LISTEN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voice listening test</title>
<style>
:root{--bg:#faf8f5;--card:#fff;--text:#1d1b19;--muted:#6f6a63;--border:#e6e0d7;--accent:#e8643c}
@media (prefers-color-scheme:dark){:root{--bg:#151412;--card:#1e1c1a;--text:#f3efe9;--muted:#a29b92;--border:#34302c;--accent:#f07a54}}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 64px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px;margin:14px 0}
h1{margin:0 0 4px}.muted{color:var(--muted)}
.line{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0}.line span{flex:1;min-width:200px}
audio{height:32px;max-width:100%}
.ratings{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:10px}
select,input{font:inherit;padding:6px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text)}
button{font:inherit;border:0;border-radius:999px;padding:10px 18px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}
</style></head><body><main>
<h1>Which voice would you talk to every day?</h1>
<p class="muted">Voices are unlabeled on purpose. Listen to each one's lines, rate it 1 (bad) to 5 (great), then pick a favorite and download your ratings.</p>
<label>Your name <input id="rater" placeholder="optional"></label>
<div id="voices"></div>
<div class="card"><strong>Which one would you want to talk to every day?</strong>
<div class="ratings"><select id="favorite"></select></div></div>
<button id="save">Download my ratings</button>
</main><script>
const VOICES=__VOICES__, LINES=__LINES__;
const root=document.getElementById('voices'), fav=document.getElementById('favorite');
const opts=[1,2,3,4,5].map(n=>`<option>${n}</option>`).join('');
for(const v of VOICES){
  const d=document.createElement('div');d.className='card';
  d.innerHTML=`<strong>${v.label}</strong>`+LINES.map(l=>`<div class="line"><span class="muted">${l.text}</span><audio controls preload="none" src="${v.slug}/${l.key}.wav"></audio></div>`).join('')+
  `<div class="ratings">${['natural','warm','clear'].map(k=>`<label>${k[0].toUpperCase()+k.slice(1)} <select data-v="${v.slug}" data-k="${k}"><option value="">–</option>${opts}</select></label>`).join('')}</div>`;
  root.appendChild(d); fav.insertAdjacentHTML('beforeend',`<option value="${v.slug}">${v.label}</option>`);
}
document.getElementById('save').onclick=()=>{
  const r={rater:document.getElementById('rater').value||'anonymous',favorite:fav.value,scores:{}};
  document.querySelectorAll('select[data-v]').forEach(s=>{if(s.value){(r.scores[s.dataset.v]??={})[s.dataset.k]=+s.value}});
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(r,null,2)],{type:'application/json'}));
  a.download=`ratings-${r.rater}.json`;a.click();
};
</script></body></html>
"""


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------


def score(files: list[str]) -> None:
    rendered = {r["slug"]: r for r in json.loads((OUT / "render.json").read_text())}
    agg: dict[str, dict[str, list[int]]] = {slug: {"natural": [], "warm": [], "clear": []} for slug in rendered}
    favorites: dict[str, int] = {slug: 0 for slug in rendered}
    for f in files:
        data = json.loads(Path(f).read_text())
        favorites[data.get("favorite", "")] = favorites.get(data.get("favorite", ""), 0) + 1
        for slug, scores in data.get("scores", {}).items():
            for k, val in scores.items():
                agg.setdefault(slug, {"natural": [], "warm": [], "clear": []})[k].append(val)

    def avg(xs: list[int]) -> str:
        return f"{statistics.mean(xs):.1f}" if xs else "–"

    rows = sorted(rendered.values(), key=lambda r: -favorites.get(r["slug"], 0))
    print(f"Tested {len(rendered)} voices with {len(files)} listeners.\n")
    print("| Voice | Natural (1-5) | Warm (1-5) | Clear (1-5) | Favorite votes | Time to first audio (median) |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        a = agg[r["slug"]]
        print(f"| {r['spec']} | {avg(a['natural'])} | {avg(a['warm'])} | {avg(a['clear'])} | "
              f"{favorites.get(r['slug'], 0)} | {r['ttfb_ms']} ms |")


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["help"]
    settings = Settings()
    if cmd == "list":
        list_voices(settings)
    elif cmd == "render":
        render(settings, args)
    elif cmd == "score":
        score(args)
    else:
        print(__doc__)
