"""#16: the numbers behind the product: onboarding funnel, latency, cost. No conversation content.

    GET /api/metrics?days=30   (requires ?token= when METRICS_TOKEN is set)
"""

from __future__ import annotations

import statistics
import time
from typing import Any

from .state import OnboardingState

# $ per million tokens (input, output). Cache reads bill 0.1x input; 5-minute cache writes 1.25x.
PRICES = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-5": (5.0, 25.0),
}


def turn_cost(model: str, usage: dict[str, int]) -> float | None:
    if model not in PRICES:
        return None
    p_in, p_out = PRICES[model]
    return (
        usage.get("input_tokens", 0) * p_in
        + usage.get("output_tokens", 0) * p_out
        + usage.get("cache_read_input_tokens", 0) * p_in * 0.1
        + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25
    ) / 1e6


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))])


FUNNEL = [
    ("Opened Persona", lambda s: True),
    ("Named the assistant", lambda s: bool(s.agent_name)),
    ("Took the call", lambda s: any(t.role == "event" and t.text == "Call connected" for t in s.transcript)),
    ("Shared what they need", lambda s: bool(s.help_topic)),
    ("Connected Gmail", lambda s: s.gmail_status == "connected" or any(
        t.role == "event" and t.text in ("Gmail connected", "Demo data connected") for t in s.transcript)),
    ("Got into the app", lambda s: s.graduated),
]


def compute(turns: list[dict[str, Any]], sessions: list[OnboardingState]) -> dict[str, Any]:
    real = [s for s in sessions if s.user_turns > 0 or s.graduated]  # ignore sessions nobody used
    funnel = []
    prev = None
    for step, test in FUNNEL:
        n = sum(1 for s in real if test(s))
        funnel.append({"step": step, "count": n, "drop_pct": None if prev in (None, 0) else round(100 * (prev - n) / prev)})
        prev = n

    voice_audio = [t["first_audio_ms"] for t in turns if t["channel"] == "voice" and t["first_audio_ms"] is not None]
    ttft = [t["ttft_ms"] for t in turns if t["ttft_ms"] is not None]
    edges = [0, 500, 750, 1000, 1250, 1500, 2000, 3000]
    hist = []
    for lo, hi in zip(edges, edges[1:] + [None]):
        n = sum(1 for v in voice_audio if v >= lo and (hi is None or v < hi))
        hist.append({"from": lo, "to": hi, "count": n})

    per_session: dict[str, float] = {}
    for t in turns:
        if t["cost_usd"] is not None:
            per_session[t["session_id"]] = per_session.get(t["session_id"], 0) + t["cost_usd"]
    by_model = []
    for model in sorted({t["model"] for t in turns}):
        ts = [t for t in turns if t["model"] == model]
        costs = [t["cost_usd"] for t in ts if t["cost_usd"] is not None]
        by_model.append({
            "model": model,
            "turns": len(ts),
            "ttft_p50": _pct([t["ttft_ms"] for t in ts if t["ttft_ms"] is not None], 0.5),
            "cost_per_turn": round(statistics.mean(costs), 5) if costs else None,
        })
    cache_read = sum(t["cache_read_tokens"] or 0 for t in turns)
    fresh = sum((t["input_tokens"] or 0) + (t["cache_write_tokens"] or 0) for t in turns)

    return {
        "generated_at": time.time(),
        "kpis": {
            "sessions": len(real),
            "graduated_pct": round(100 * sum(s.graduated for s in real) / len(real)) if real else None,
            "voice_first_audio_p50": _pct(voice_audio, 0.5),
            "voice_first_audio_p90": _pct(voice_audio, 0.9),
            "llm_first_token_p50": _pct(ttft, 0.5),
            "llm_first_token_p90": _pct(ttft, 0.9),
            "cost_per_conversation": round(statistics.mean(per_session.values()), 4) if per_session else None,
            "cache_hit_pct": round(100 * cache_read / (cache_read + fresh)) if (cache_read + fresh) else None,
            "turns": len(turns),
            "voice_turns_with_audio": len(voice_audio),
        },
        "funnel": funnel,
        "voice_latency_hist": hist,
        "by_model": by_model,
    }
