"""The deterministic orchestrator: code owns control flow, the model owns language.

Responsibilities:
  * validate and apply every save_onboarding_info call
  * track behavioral signals (short-answer streaks, stalls)
  * enforce the graduation rule
  * write the director's note that tells the model what to focus on this turn
  * apply app events (call accepted, hangup, Gmail connected, ...)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import validation as v
from .state import DEFAULT_AGENT_NAME, OnboardingState, Turn

GRADUATION_COOLDOWN_TURNS = 3
STALL_TURNS = 3
CHATTY_WORDS = 60


@dataclass
class ApplyResult:
    """Outcome of one tool call: what to tell the model, and what to tell the UI."""

    messages: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    ui: list[dict[str, Any]] = field(default_factory=list)
    progressed: bool = False

    def tool_result_text(self) -> str:
        parts = self.messages + [f"REJECTED {r}" for r in self.rejected]
        return "; ".join(parts) if parts else "ok"


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def graduation_allowed(state: OnboardingState) -> bool:
    """Plan 4.6: help topic known AND at least one of user name or Gmail."""
    return bool(state.help_topic) and bool(state.user_name or state.gmail)


def graduation_cooling_down(state: OnboardingState) -> bool:
    d = state.graduation_declined_at_turn
    return d is not None and state.user_turns - d < GRADUATION_COOLDOWN_TURNS


def in_call(state: OnboardingState) -> bool:
    return state.call_status == "in_progress"


def value_moment_due(state: OnboardingState) -> bool:
    return state.gmail_status == "connected" and bool(state.help_topic) and not state.value_moment_done and not state.graduated


def call_offer_due(state: OnboardingState) -> bool:
    """The on-screen call offer appears once the agent has a name (journey step 2)."""
    return state.channel == "text" and state.call_status == "not_started" and bool(state.agent_name) and not state.graduated


def gmail_still_offerable(state: OnboardingState) -> bool:
    return state.gmail_status in ("not_connected", "error") and state.gmail_offer_count < 2


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


def _set_slot(state: OnboardingState, slot: str, value: str, res: ApplyResult) -> None:
    old = getattr(state, slot)
    if old == value:
        return
    was_default = slot == "agent_name" and state.agent_name_defaulted
    setattr(state, slot, value)
    if slot == "agent_name":
        state.agent_name_defaulted = False
    if old and not was_default:
        if slot not in state.corrected:
            state.corrected.append(slot)
        res.messages.append(f"updated {slot}: {old!r} -> {value!r}")
    else:
        res.messages.append(f"saved {slot}={value!r}")
    res.progressed = True
    res.ui.append({"type": "slot", "slot": slot, "value": value})


def apply_tool_call(state: OnboardingState, args: dict[str, Any]) -> ApplyResult:
    res = ApplyResult()
    if not isinstance(args, dict):
        res.rejected.append("input was not an object")
        return res
    checks = {
        "user_name": v.check_person_name,
        "agent_name": v.check_agent_name,
        "help_topic": v.check_help_topic,
    }
    for slot, check in checks.items():
        raw = args.get(slot)
        if not isinstance(raw, str) or not raw.strip():
            continue
        value, reason = check(raw)
        if value is None:
            res.rejected.append(f"{slot}={raw!r}: {reason}")
        else:
            _set_slot(state, slot, value, res)

    sentiment = args.get("sentiment")
    if sentiment in ("neutral", "frustrated", "enthusiastic", "rushed", "chatty") and sentiment != state.sentiment:
        state.sentiment = sentiment
        res.messages.append(f"sentiment={sentiment}")

    wants_call = args.get("wants_call")
    if wants_call is True and state.call_status in ("not_started", "offered", "declined", "missed", "hung_up"):
        res.ui.append({"type": "start_call"})
        res.messages.append("call is starting; the phone will ring on their screen")
    elif wants_call is False and state.call_status in ("not_started", "offered"):
        state.call_status = "declined"
        state.channel = "text"
        res.ui.append({"type": "hide_call_offer"})
        res.messages.append("call declined; continuing over text")

    if args.get("wants_text") and in_call(state):
        end_call(state, reason="switched_to_text")
        res.ui.append({"type": "end_call", "reason": "switched_to_text"})
        res.messages.append("call ended; continuing over text")

    if args.get("show_gmail_button") and state.gmail_status != "connected":
        if not state.gmail_card_shown:
            state.gmail_offer_count += 1
        state.gmail_card_shown = True
        res.ui.append({"type": "show_gmail_card"})
        res.messages.append("Connect Gmail button is now on screen")

    if args.get("declined_gmail") and state.gmail_status != "connected":
        state.gmail_status = "denied"
        state.gmail_card_shown = False
        res.ui.append({"type": "hide_gmail_card"})
        res.messages.append("noted: no Gmail for now")

    if args.get("offered_graduation"):
        state.graduation_offered = True

    answer = args.get("graduation_answer")
    if answer == "declined":
        state.graduation_declined_at_turn = state.user_turns
        res.messages.append("graduation declined; won't offer again for a few turns")
    elif answer == "accepted" or args.get("wants_to_skip"):
        graduate(state, res)

    return res


def graduate(state: OnboardingState, res: ApplyResult | None = None) -> None:
    if state.graduated:
        return
    state.graduated = True
    if not state.agent_name:
        state.agent_name = DEFAULT_AGENT_NAME
        state.agent_name_defaulted = True
    if in_call(state):
        state.call_status = "completed"
    if res is not None:
        res.messages.append("graduated; say a warm one-line goodbye")
        res.ui.append({"type": "graduated"})


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def before_user_turn(state: OnboardingState, text: str) -> None:
    """Behavioral signals computed from the user's message before the model sees it."""
    state.user_turns += 1
    state.turns_since_progress += 1
    if v.is_short_answer(text):
        state.short_answer_streak += 1
    else:
        state.short_answer_streak = 0


def after_turn(state: OnboardingState, progressed: bool) -> None:
    if progressed:
        state.turns_since_progress = 0


# ---------------------------------------------------------------------------
# App events
# ---------------------------------------------------------------------------


def start_call(state: OnboardingState) -> None:
    if not state.agent_name:
        state.agent_name = DEFAULT_AGENT_NAME
        state.agent_name_defaulted = True
    state.call_status = "ringing"


def end_call(state: OnboardingState, reason: str) -> None:
    state.channel = "text"
    state.call_status = {
        "hangup": "hung_up",
        "missed": "missed",
        "switched_to_text": "completed",
        "completed": "completed",
    }.get(reason, "hung_up")


def add_event_turn(state: OnboardingState, text: str) -> None:
    state.transcript.append(Turn(role="event", text=text, channel=state.channel))


# ---------------------------------------------------------------------------
# Director's note
# ---------------------------------------------------------------------------


def _priority(state: OnboardingState) -> list[str]:
    """What the model should focus on this turn, most important first."""
    s = state
    lines: list[str] = []

    if s.graduated:
        return ["They've graduated. Wrap up warmly in one sentence; don't ask for anything."]

    if s.channel == "text" and not s.agent_name and not in_call(s):
        return [
            "Learn what they want to call you. If they're unsure, suggest two or three short, friendly names. "
            f"If they want to skip, that's fine: save agent_name={DEFAULT_AGENT_NAME!r}. "
            "As soon as you have a name, react to it in a few words and, in the same reply, ask if they're up for a "
            "quick two-minute call right here in the browser, or would rather keep texting."
        ]

    if s.channel == "text" and s.call_status == "not_started":
        name = s.agent_name or DEFAULT_AGENT_NAME
        return [
            f"React to the name {name!r} in a few words, then ask if they're up for a quick two-minute call "
            "(you'll call them right here in the browser), or they can keep texting. Make both options feel fine. "
            "Their answer goes in wants_call."
        ]

    frustrated = s.sentiment == "frustrated"
    rushed = s.sentiment == "rushed" or s.short_answer_streak >= 2

    if frustrated:
        lines.append(
            "They're frustrated. Apologize briefly, no small talk. Ask for only the single most important missing piece"
            + (" (what they need help with)" if not s.help_topic else "")
            + ", and offer to switch to text or skip for now."
        )

    grad_ok = graduation_allowed(s) and not graduation_cooling_down(s)
    if grad_ok and (rushed or frustrated):
        lines.append(
            "Offer to let them jump in now; Gmail and anything else can be picked up later."
            if s.gmail_status != "connected"
            else "Offer to let them jump in now."
        )
        return lines

    if s.gmail_status == "connected" and s.help_topic and not s.value_moment_done:
        lines.append(
            "Value moment: give one specific, useful suggestion tied to what they need help with, and offer "
            "(don't do) one concrete next action. Gmail data isn't wired up yet, so base it on what they told you "
            "and don't invent calendar events or emails."
        )
        return lines

    if not s.user_name:
        lines.append("Learn what to call them.")
    elif not s.help_topic:
        lines.append("Learn what they could use a hand with, in their own words. Ask it in an inviting way, not like a survey.")
    elif gmail_still_offerable(s) and not s.gmail_card_shown:
        lines.append(
            "Suggest connecting Gmail and tie it to what they need help with. Tell them the Connect Gmail button is "
            "on their screen now (set show_gmail_button=true). Mention Google will show a warning because this is a "
            "test app: tap Advanced, then continue. Make it clearly optional."
        )
    elif s.gmail_card_shown and s.gmail_status in ("not_connected", "popup_open"):
        lines.append("The Connect Gmail button is on their screen. Don't push; keep chatting while they decide.")
        if grad_ok and not s.graduation_offered:
            lines.append("If it feels natural, offer to let them jump in; Gmail can be connected later.")
    elif grad_ok and not s.graduation_offered:
        lines.append("Offer to let them jump in now, phrased as a question. Don't force it.")
    elif grad_ok:
        lines.append("Everything essential is done. Keep it brief; if they seem ready, offer again to jump in.")

    if s.turns_since_progress >= STALL_TURNS and not s.graduated:
        lines.append(
            f"The last {s.turns_since_progress} turns haven't moved things forward. Gently nudge, or offer to "
            "finish later / skip for now."
        )
    return lines or ["Keep the conversation natural."]


def directors_note(state: OnboardingState, *, channel: str, user_text: str = "") -> str:
    s = state
    filled = ", ".join(f"{k}={val!r}" for k, val in s.filled().items()) or "nothing yet"
    missing = ", ".join(s.missing()) or "nothing"
    if s.agent_name_defaulted:
        filled += " (agent_name is the default; they skipped naming)"

    signals: list[str] = []
    if s.sentiment != "neutral":
        signals.append(f"mood reads {s.sentiment}")
    if s.short_answer_streak >= 2:
        signals.append(f"short answers for {s.short_answer_streak} turns (maybe rushed)")
    if len(user_text.split()) > CHATTY_WORDS:
        signals.append("long message (let them finish, react to something specific, bridge back in one line)")
    if s.corrected:
        signals.append(f"corrected earlier: {', '.join(s.corrected)}")

    if s.graduated:
        grad = "done"
    elif not graduation_allowed(s):
        grad = "not yet allowed"
    elif graduation_cooling_down(s):
        grad = "allowed, but they just declined; don't offer again yet"
    elif s.graduation_offered:
        grad = "allowed, already offered"
    else:
        grad = "allowed, not yet offered"

    call = {
        "in_progress": "on a call right now",
        "hung_up": "the call dropped earlier; now texting",
        "declined": "they chose text over a call",
        "missed": "they missed the call; now texting",
        "completed": "call finished; now texting",
    }.get(s.call_status, s.call_status)

    length = "One or two short sentences." if channel == "voice" else "Keep it short, like a text message."
    if s.sentiment in ("rushed", "frustrated") or s.short_answer_streak >= 2:
        length = "One sentence."

    lines = [
        f"Channel: {channel} ({call})",
        f"Filled: {filled}",
        f"Missing: {missing}",
        f"Gmail: {s.gmail_status}" + (" (button on screen)" if s.gmail_card_shown and s.gmail_status != "connected" else ""),
        f"Signals: {'; '.join(signals) if signals else 'none'}",
        f"Graduation: {grad}",
        "Priority: " + " ".join(_priority(s)),
        length,
    ]
    return "<director_note>\n" + "\n".join(lines) + "\n</director_note>"
