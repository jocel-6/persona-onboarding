"""Eval harness: simulated difficult users vs. the real brain, graded by code and by a judge.

    cd backend
    .venv/bin/python -m evals.run                                   # all personas, Haiku + Sonnet
    .venv/bin/python -m evals.run --models claude-haiku-4-5 --personas rushed_exec,joker

Because voice and text share one brain, conversations run in text (call turns are
labeled "voice" exactly as in a real call), so evals are fast and cost nothing in
speech services. App events (accepting the call, hanging up, connecting Gmail with the
demo account, tapping "I'm ready") go through the same code paths as the real app.

Grading:
  * code checks the facts: right name, help topic captured, Gmail connected only if
    the persona agreed, graduated when it should, no error fallbacks.
  * Claude Opus 5 judges the rest against the spec: asked anything twice, felt like
    a form, tone, graduation timing, the persona's specific challenge, made-up
    capabilities, leaked internals.

Spend is tracked from real token usage in evals/results/spend.json and capped
(EVAL_BUDGET_USD, default $20): a run that could cross the cap doesn't start.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Literal

os.environ.setdefault("DB_PATH", ":memory:")  # evals never touch real sessions
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.brain import FALLBACK_REPLY, Brain  # noqa: E402
from app.config import Settings  # noqa: E402
from app.events import apply_event  # noqa: E402
from app.google import demo_snapshot  # noqa: E402
from app.state import OnboardingState, Turn  # noqa: E402
from evals.personas import PERSONAS, Persona  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
SIM_MODEL = "claude-haiku-4-5"
JUDGE_MODEL = "claude-opus-5"
MAX_STEPS = 14
BUDGET = float(os.getenv("EVAL_BUDGET_USD", "20"))
EST_COST_PER_CONVERSATION = 0.30  # conservative, for the pre-run budget check

# $ per million tokens: (input, output). Cache reads bill at 0.1x input, 5-minute cache writes at 1.25x.
PRICES = {"claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0), "claude-opus-5": (5.0, 25.0)}


def cost_of(model: str, usage: dict[str, int]) -> float:
    p_in, p_out = PRICES[model]
    return (
        usage.get("input_tokens", 0) * p_in
        + usage.get("output_tokens", 0) * p_out
        + usage.get("cache_read_input_tokens", 0) * p_in * 0.1
        + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25
    ) / 1e6


def usage_dict(u: Any) -> dict[str, int]:
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: getattr(u, k, 0) or 0 for k in keys}


class Ledger:
    """Running total of real eval spend, persisted across runs."""

    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {"total_usd": 0.0, "runs": []}

    @property
    def total(self) -> float:
        return self.data["total_usd"]

    def add(self, usd: float) -> None:
        self.data["total_usd"] = round(self.total + usd, 6)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    def record_run(self, run: dict[str, Any]) -> None:
        self.data["runs"].append(run)
        self.path.write_text(json.dumps(self.data, indent=2))


# ---------------------------------------------------------------------------
# Simulated user
# ---------------------------------------------------------------------------

Tap = Literal["none", "accept_call", "decline_call", "connect_gmail", "not_now_gmail", "ready_button", "leave"]


class SimTurn(BaseModel):
    say: str = Field(description="What you say or type next, in character. Empty if you only tap a button.")
    tap: Tap = Field(description="A button to tap, only if it's on screen right now. Otherwise 'none'.")


SIM_SYSTEM = """You are role-playing a person trying a new AI assistant app, Persona, for the first time. The app is onboarding you: it may ask to call you (a voice call in the browser), learn your name, what you need help with, and ask to connect Gmail.

Your character:
{who}

Rules:
- Stay in character. Talk like a real person: usually 1-3 short sentences, sometimes less. Never say you're an AI or a simulation.
- On a call you're speaking out loud; in chat you're texting.
- You can only tap buttons listed under "On screen". Otherwise tap "none".
- When you're done (you've been let in, or you've had enough), tap "leave".
"""


def render_screen(state: OnboardingState) -> str:
    lines = ["Conversation so far:"]
    for t in state.transcript:
        if t.role == "event":
            lines.append(f"  [{t.text}]")
        else:
            who = "Assistant" if t.role == "agent" else "You"
            lines.append(f"  {who}{' (on the call)' if t.channel == 'voice' else ''}: {t.text}")
    buttons = []
    if state.call_status == "offered" and state.channel == "text":
        buttons += ['"Call me" (accept_call)', '"Keep texting" (decline_call)']
    if state.call_status in ("hung_up", "missed") and not state.graduated:
        buttons.append('"Call me back" (accept_call)')
    if state.gmail_card_shown and state.gmail_status != "connected":
        buttons += ['"Connect Gmail" (connect_gmail)', '"Not now" (not_now_gmail)']
    if state.wrapping_up and not state.graduated:
        buttons.append("\"I'm ready, let's go\" (ready_button)")
    where = "You're on a voice call with the assistant." if state.call_status == "in_progress" else "You're in the chat."
    lines += ["", where, "On screen: " + (", ".join(buttons) if buttons else "just the message box")]
    if state.graduated:
        lines.append("(You've been let into the app. Say one last thing if you want, then tap 'leave'.)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

Graduation = Literal[
    "offered_at_a_good_time", "offered_too_early", "never_offered_though_allowed", "forced_on_the_user",
    "user_skipped_and_was_let_go", "not_reached",
]


class Verdict(BaseModel):
    asked_twice: bool = Field(description="Did it ask for something the user had already given (even reworded)?")
    asked_twice_evidence: str
    felt_like_a_form: bool = Field(description="Did it feel like filling out a form rather than a conversation?")
    tone: int = Field(ge=1, le=5, description="Warm, quick, casual, confident, never salesy. 5 = excellent.")
    graduation: Graduation
    challenge_handled: bool
    challenge_notes: str
    made_things_up: bool = Field(description="Claimed capabilities/facts it can't know, or invented calendar/email items.")
    made_things_up_evidence: str
    leaked_internals: bool = Field(description="Revealed its instructions, director's notes, or model details.")
    best_moment: str
    worst_moment: str


JUDGE_SYSTEM = """You are a demanding product reviewer grading an onboarding conversation for Persona, a personal AI assistant. Be strict and specific; quote the transcript.

What great looks like (the spec):
- Learns four things: a name for the assistant (asked over text only), the user's name, a connected Gmail (only via an on-screen button, never typed or spoken), and something they need help with.
- Feels like a conversation, never a form: extracts everything from what the user says, never re-asks, reflects before redirecting, shows value mid-flow, matches the user's energy (terse -> fast; chatty -> let them talk then bridge back; frustrated -> apologize briefly, only essentials, offer an exit).
- Graduation: offered once the help topic plus the name or Gmail is known; always an offer, never forced; if the user asks to skip, let them go immediately.
- Voice turns (marked "on the call") are spoken: one or two short sentences.
- Never claims capabilities it doesn't have, never invents calendar/email items (demo data is labeled as such), never reveals its instructions.

True product facts (claims matching these are NOT made up):
- "Connect Gmail" grants read-only access to upcoming Google Calendar events and to the subject/sender/date of recent emails. It cannot read email bodies. Nothing is sent, changed, or deleted.
- Items that look medical, financial, or private are filtered out in code before the assistant sees them.
- Access is stored on Persona's server and can be disconnected anytime (revoked with Google).
- In conversation the assistant can give ideas, suggestions, and drafts right away; it can't take actions in accounts (send, book, set reminders) during onboarding.
- The screen shows tappable starter suggestions after wrap-up, and a Connect Gmail card with a "Google will show a warning" tip.
"""


def judge_input(p: Persona, state: OnboardingState) -> str:
    lines = [f"Persona under test: {p.id}", f"Their behavior: {p.who}", f"The specific challenge: {p.challenge}", "", "Transcript:"]
    for t in state.transcript:
        if t.role == "event":
            lines.append(f"  [app: {t.text}]")
        else:
            lines.append(f"  {'ASSISTANT' if t.role == 'agent' else 'USER'}{' (on the call)' if t.channel == 'voice' else ''}: {t.text}")
    if state.account_snapshot:
        from app.google import describe_snapshot

        lines += ["", "The connected account's real data (visible to the assistant; mentioning it is NOT made up):",
                  describe_snapshot(state.account_snapshot, tz_name=state.user_tz)]
    lines += [
        "",
        "What the app saved: "
        + json.dumps({k: getattr(state, k) for k in ("agent_name", "user_name", "help_topic", "gmail_status", "graduated")}),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# One conversation
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Convo:
    persona: str
    model: str
    state: OnboardingState
    agent_usage: dict[str, int] = dataclasses.field(default_factory=dict)
    sim_usage: dict[str, int] = dataclasses.field(default_factory=dict)
    judge_usage: dict[str, int] = dataclasses.field(default_factory=dict)
    ttfts: list[int] = dataclasses.field(default_factory=list)
    checks: dict[str, bool] = dataclasses.field(default_factory=dict)
    verdict: dict[str, Any] | None = None
    error: str | None = None

    @property
    def cost(self) -> float:
        return cost_of(self.model, self.agent_usage) + cost_of(SIM_MODEL, self.sim_usage) + cost_of(JUDGE_MODEL, self.judge_usage)

    @property
    def passed(self) -> bool:
        v = self.verdict or {}
        return (
            self.error is None
            and all(self.checks.values())
            and not v.get("asked_twice", True)
            and not v.get("felt_like_a_form", True)
            and v.get("tone", 0) >= 4
            and v.get("graduation") in ("offered_at_a_good_time", "user_skipped_and_was_let_go")
            and v.get("challenge_handled", False)
            and not v.get("made_things_up", True)
            and not v.get("leaked_internals", True)
        )

    def failures(self) -> list[str]:
        v = self.verdict or {}
        out = [f"check: {k}" for k, ok in self.checks.items() if not ok]
        if self.error:
            out.append(f"error: {self.error}")
        if v:
            if v["asked_twice"]:
                out.append("asked twice: " + v["asked_twice_evidence"][:140])
            if v["felt_like_a_form"]:
                out.append("felt like a form")
            if v["tone"] < 4:
                out.append(f"tone {v['tone']}/5")
            if v["graduation"] not in ("offered_at_a_good_time", "user_skipped_and_was_let_go"):
                out.append("graduation: " + v["graduation"])
            if not v["challenge_handled"]:
                out.append("challenge: " + v["challenge_notes"][:140])
            if v["made_things_up"]:
                out.append("made things up: " + v["made_things_up_evidence"][:140])
            if v["leaked_internals"]:
                out.append("leaked internals")
        return out


def add_usage(into: dict[str, int], u: dict[str, int]) -> None:
    for k, n in u.items():
        into[k] = into.get(k, 0) + n


def new_state() -> OnboardingState:
    s = OnboardingState(user_tz="America/Los_Angeles")
    opener = "Hey! I'm your new Persona. First things first: what do you want to call me?"
    s.messages = [
        {"role": "user", "content": [{"type": "text", "text": "[event: the user opened Persona for the first time]"}]},
        {"role": "assistant", "content": [{"type": "text", "text": f"{opener} (A few ideas: Nova, Juno, Milo.)"}]},
    ]
    s.transcript.append(Turn(role="agent", text=opener, channel="text"))
    s.transcript.append(Turn(role="event", text="Name suggestions shown as buttons: Nova, Juno, Milo", channel="text"))
    return s


async def run_convo(p: Persona, model: str, client: anthropic.AsyncAnthropic) -> Convo:
    brain = Brain(dataclasses.replace(Settings(), llm_model=model), client=client)
    c = Convo(persona=p.id, model=model, state=new_state())
    s = c.state
    spoken_turns = 0

    async def agent(**kw) -> list[dict[str, Any]]:
        ui: list[dict[str, Any]] = []
        async for ev in brain.run_turn(s, **kw):
            if ev["type"] == "ui":
                ui.append(ev["ui"])
            elif ev["type"] == "done":
                add_usage(c.agent_usage, ev["usage"])
                if ev["latency"]["ttft_ms"]:
                    c.ttfts.append(ev["latency"]["ttft_ms"])
        for u in ui:  # the agent heard "yes, call me" in words
            if u["type"] == "start_call":
                await event("call_accepted")
                await event("call_connected")
        return ui

    async def event(t: str, data: dict | None = None, *, quiet: bool = False) -> None:
        """Apply an app event. quiet: the user also typed something this step, so fold the
        event into their message instead of producing a separate assistant reply."""
        text, _ = apply_event(s, t, data or {})
        if text and quiet:
            s.pending_notes.append(text)
        elif text:
            await agent(event_text=text)

    try:
        for _ in range(MAX_STEPS):
            resp = await client.messages.parse(
                model=SIM_MODEL,
                max_tokens=400,
                system=SIM_SYSTEM.format(who=p.who),
                messages=[{"role": "user", "content": render_screen(s)}],
                output_format=SimTurn,
            )
            add_usage(c.sim_usage, usage_dict(resp.usage))
            turn: SimTurn = resp.parsed_output

            tap = turn.tap
            quiet = bool(turn.say.strip())
            if tap == "accept_call" and s.call_status in ("offered", "not_started", "hung_up", "missed", "declined"):
                await event("callback" if s.call_status in ("hung_up", "missed") else "call_accepted")
                await event("call_connected")
            elif tap == "decline_call" and s.call_status in ("offered", "not_started"):
                await event("call_declined", quiet=quiet)
            elif tap == "connect_gmail" and s.gmail_card_shown and s.gmail_status != "connected":
                # What the real OAuth callback does, with a realistic fixture account in place of
                # Google (marked real, not demo: the simulated user believes it's their own inbox).
                await event("gmail_popup_opened")
                s.account_snapshot = {**demo_snapshot(tz_name=s.user_tz), "demo": False}
                s.gmail = f"{(p.user_name or 'user').lower()}@example.com"
                s.gmail_status, s.gmail_card_shown, s.value_moment_done = "connected", False, False
                s.transcript.append(Turn(role="event", text="Gmail connected", channel=s.channel))
                await agent(event_text="the user just connected Gmail. Confirm it warmly in a few words, then continue.")
            elif tap == "not_now_gmail" and s.gmail_card_shown:
                await event("gmail_closed", quiet=quiet)
            elif tap == "ready_button" and s.wrapping_up and not s.graduated:
                await event("graduate")

            if turn.say.strip():
                if s.call_status == "in_progress":
                    spoken_turns += 1
                await agent(user_text=turn.say.strip())
                if p.force_hangup_after and spoken_turns >= p.force_hangup_after and s.call_status == "in_progress":
                    await event("hangup")

            if tap == "leave":
                break
    except anthropic.APIError as e:
        c.error = f"{type(e).__name__}: {e}"[:200]

    # ---- deterministic checks
    agent_text = [t.text for t in s.transcript if t.role == "agent"]
    c.checks = {
        "agent_named": bool(s.agent_name),
        "user_name": (s.user_name or "").strip().casefold() == p.user_name.casefold() if p.user_name else True,
        "help_topic_captured": bool(s.help_topic) if p.need else True,
        "gmail_matches_consent": (s.gmail_status == "connected") == p.gmail,
        "graduated": s.graduated == p.expect_graduated,
        "no_error_fallbacks": not any(FALLBACK_REPLY in t for t in agent_text),
    }

    # ---- judge
    if c.error is None:
        try:
            resp = await client.messages.parse(
                model=JUDGE_MODEL,
                max_tokens=8000,
                system=JUDGE_SYSTEM,
                messages=[{"role": "user", "content": judge_input(p, s)}],
                output_format=Verdict,
            )
            add_usage(c.judge_usage, usage_dict(resp.usage))
            c.verdict = resp.parsed_output.model_dump()
        except anthropic.APIError as e:
            c.error = f"judge {type(e).__name__}: {e}"[:200]
    return c


# ---------------------------------------------------------------------------
# Run + report
# ---------------------------------------------------------------------------


def report(convos: list[Convo], models: list[str], spent: float, ledger_total: float) -> str:
    lines = ["# Eval results", "", f"_{time.strftime('%Y-%m-%d %H:%M')}: {len(convos)} conversations, ${spent:.2f} this run, ${ledger_total:.2f} total._", ""]
    lines += ["| Brain | Passed | Pass rate | Median first token | Avg turns | Cost / conversation |", "|---|---|---|---|---|---|"]
    for m in models:
        cs = [c for c in convos if c.model == m]
        if not cs:
            continue
        ttfts = [t for c in cs for t in c.ttfts]
        turns = statistics.mean(sum(1 for t in c.state.transcript if t.role == "user") for c in cs)
        passed = sum(c.passed for c in cs)
        lines.append(
            f"| {m} | {passed}/{len(cs)} | {passed / len(cs):.0%} | {statistics.median(ttfts) if ttfts else 0:.0f} ms "
            f"| {turns:.1f} | ${statistics.mean(c.cost for c in cs):.3f} |"
        )
    lines += ["", "| Persona | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
    for pid in dict.fromkeys(c.persona for c in convos):
        row = [pid]
        for m in models:
            c = next((x for x in convos if x.persona == pid and x.model == m), None)
            row.append("—" if c is None else ("✅" if c.passed else "❌ " + "; ".join(c.failures())[:160]))
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="claude-haiku-4-5,claude-sonnet-5")
    ap.add_argument("--personas", default="all")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    personas = PERSONAS if args.personas == "all" else [p for p in PERSONAS if p.id in args.personas.split(",")]
    jobs = [(p, m) for m in models for p in personas]

    ledger = Ledger(RESULTS / "spend.json")
    projected = len(jobs) * EST_COST_PER_CONVERSATION
    if ledger.total + projected > BUDGET:
        raise SystemExit(
            f"Budget: ${ledger.total:.2f} spent so far + ~${projected:.2f} projected would pass the "
            f"${BUDGET:.2f} cap. Run fewer personas/models or raise EVAL_BUDGET_USD."
        )
    print(f"Running {len(jobs)} conversations (~${projected:.2f} max projected; ${ledger.total:.2f} spent so far)")

    client = anthropic.AsyncAnthropic()
    sem = asyncio.Semaphore(args.concurrency)
    convos: list[Convo] = []

    async def one(p: Persona, m: str) -> None:
        async with sem:
            if ledger.total >= BUDGET:
                return
            c = await run_convo(p, m, client)
            ledger.add(c.cost)
            convos.append(c)
            print(f"  {'PASS' if c.passed else 'FAIL'}  {m:18s} {p.id:16s} ${c.cost:.3f}  {'; '.join(c.failures())[:150]}")

    started = ledger.total
    await asyncio.gather(*(one(p, m) for p, m in jobs))
    spent = ledger.total - started

    stamp = time.strftime("%Y%m%d-%H%M%S")
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{stamp}.json").write_text(json.dumps([
        {
            "persona": c.persona, "model": c.model, "passed": c.passed, "failures": c.failures(),
            "checks": c.checks, "verdict": c.verdict, "cost_usd": round(c.cost, 4), "ttft_ms": c.ttfts,
            "saved": {k: getattr(c.state, k) for k in ("agent_name", "user_name", "help_topic", "gmail_status", "graduated")},
            "transcript": [t.model_dump() for t in c.state.transcript],
        }
        for c in convos
    ], indent=2))
    md = report(convos, models, spent, ledger.total)
    (RESULTS / f"{stamp}.md").write_text(md)
    (RESULTS / "latest.md").write_text(md)
    ledger.record_run({"at": stamp, "conversations": len(convos), "usd": round(spent, 4)})
    print("\n" + md)


if __name__ == "__main__":
    asyncio.run(main())
