"""The deterministic orchestrator: code owns control flow, the model owns language.

Responsibilities:
  * validate and apply every save_onboarding_info call
  * track behavioral signals (short-answer streaks, stalls)
  * enforce the graduation rule
  * write the director's note that tells the model what to focus on this turn
  * apply app events (call accepted, hangup, Gmail connected, ...)
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from typing import Any

from . import validation as v
from .google import describe_snapshot
from .state import DEFAULT_AGENT_NAME, OnboardingState, Turn

GRADUATION_COOLDOWN_TURNS = 3
STALL_TURNS = 3
CHATTY_WORDS = 60

# Ways into "what do you need?" without asking it. Ideas for the model to make
# its own, not lines to read. Each session starts at a different one and moves on
# a step each turn, so no two people get the same opener.
DISCOVERY_ANGLES = (
    "how their week has been going, and what's taken more energy than it should",
    "what's piling up for them right now: inbox, calendar, errands, family logistics, or work",
    "what they'd hand off first if they had a clone for a day",
    "something that slipped through the cracks for them recently",
    "what a normal morning or workday looks like for them, and where it goes sideways",
    "the thing they keep meaning to get to but never do",
    "the small stuff that quietly eats their time",
    "what's coming up soon that's on their mind, good or dreaded",
)


# The shape of the line, varied independently of the topic.
DISCOVERY_MOVES = (
    "a playful hypothetical",
    "a warm, specific guess about their life that they can correct",
    "a quick either/or they can pick from (or reject)",
    "a one-line peek at what you're good at, then turn it back to them",
    "a light, curious observation, then a question",
)


def _seed(state: OnboardingState) -> int:
    return zlib.crc32(state.session_id.encode())  # stable across restarts, unlike hash()


def discovery_angle(state: OnboardingState) -> str:
    return DISCOVERY_ANGLES[(_seed(state) + state.user_turns) % len(DISCOVERY_ANGLES)]


def discovery_move(state: OnboardingState) -> str:
    return DISCOVERY_MOVES[(_seed(state) // 7 + state.user_turns) % len(DISCOVERY_MOVES)]


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


def graduation_reoffer_ok(state: OnboardingState) -> bool:
    """After offering, give it a few turns before offering again (same spacing as after a no)."""
    at = state.graduation_offered_at_turn
    return at is None or state.user_turns - at >= GRADUATION_COOLDOWN_TURNS


def graduation_cooling_down(state: OnboardingState) -> bool:
    d = state.graduation_declined_at_turn
    return d is not None and state.user_turns - d < GRADUATION_COOLDOWN_TURNS


def in_call(state: OnboardingState) -> bool:
    return state.call_status == "in_progress"


def refresh_insights(state: OnboardingState) -> None:
    """Recompute what Persona noticed (after connecting, and after they fix something)."""
    from .insights import find_insights, tomorrow_at_a_glance, week_summary

    connected = state.gmail_status == "connected"
    rule_based = [
        i.public() for i in find_insights(state.account_snapshot, tz_name=state.user_tz, help_topic=state.help_topic)
    ] if connected else []
    # Deep findings lead (they're the "how did it know?" ones); rule findings follow.
    state.insights = ([*state.deep_insights, *rule_based] if connected else [])[:6]
    state.week_summary = week_summary(state.account_snapshot, tz_name=state.user_tz) if connected else None
    state.tomorrow = (
        tomorrow_at_a_glance(state.account_snapshot, state.insights, tz_name=state.user_tz) if connected else None
    )


def hunch_due(state: OnboardingState) -> bool:
    """Once we know their situation (and before their data does the talking), guess a problem they didn't mention."""
    return (
        bool(state.help_topic) and not state.hunch_done and not state.graduated and not state.wrapping_up
        and state.gmail_status != "connected" and state.sentiment not in ("rushed", "frustrated")
    )


def value_moment_due(state: OnboardingState) -> bool:
    return state.gmail_status == "connected" and bool(state.help_topic) and not state.value_moment_done and not state.graduated


def call_offer_due(state: OnboardingState) -> bool:
    """The on-screen call offer appears once the agent has a name (journey step 2)."""
    return state.channel == "text" and state.call_status == "not_started" and bool(state.agent_name) and not state.graduated


def gmail_still_offerable(state: OnboardingState) -> bool:
    return state.gmail_status in ("not_connected", "error") and state.gmail_offer_count < 2


# Gmail is what turns onboarding into real help (the "Persona noticed" moment), so once the
# button is up, give them a couple of turns with it before offering to jump in without it.
GMAIL_GRACE_TURNS = 2


def gmail_pending(state: OnboardingState) -> bool:
    """The button is on screen and they haven't connected or said no yet."""
    if not (state.gmail_card_shown and state.gmail_status in ("not_connected", "popup_open")):
        return False
    shown = state.gmail_offered_at_turn if state.gmail_offered_at_turn is not None else state.user_turns
    return state.user_turns - shown < GMAIL_GRACE_TURNS


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
        user_name = args.get("user_name") if isinstance(args.get("user_name"), str) else state.user_name
        if value is not None and slot == "agent_name" and user_name and value.casefold() == user_name.strip().casefold():
            # "Hey. Dana." at the naming step is them introducing themselves, not naming you.
            res.rejected.append(
                f"agent_name={raw!r}: that's the user's own name. Keep it as user_name and ask them to name you"
            )
        elif value is None:
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
            state.gmail_offered_at_turn = state.user_turns
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
        state.graduation_offered_at_turn = state.user_turns

    suggestions = args.get("starter_suggestions")
    if isinstance(suggestions, list):
        clean = [v.clean(x)[:80] for x in suggestions if isinstance(x, str) and v.clean(x)][:3]
        if clean:
            state.starter_suggestions = clean
            res.ui.append({"type": "suggestions", "items": clean})
            res.messages.append("starter suggestions saved")

    answer = args.get("graduation_answer")
    if args.get("ready_to_start") and not state.wrapping_up and not args.get("wants_to_skip"):
        # "I'm good" to the offer means yes: run the wrap-up before letting them go.
        answer = "accepted"
        res.rejected.append(
            "ready_to_start: the wrap-up hasn't happened yet. Their yes starts it: give the starter ideas, a tip, "
            "and ask if they have questions before saying goodbye"
        )
    if args.get("wants_to_skip") or (args.get("ready_to_start") and state.wrapping_up):
        graduate(state, res)
    elif answer == "declined":
        state.graduation_declined_at_turn = state.user_turns
        res.messages.append("graduation declined; won't offer again for a few turns")
    elif answer == "accepted" and not state.wrapping_up:
        state.wrapping_up = True
        state.graduation_offered = True
        res.ui.append({"type": "wrap_up"})
        res.messages.append("wrap-up started: starters, a tip or two, then ask if they have questions")

    return res


EDITABLE_FIELDS = {"agent_name": "agent name", "user_name": "name", "help_topic": "what they need help with"}


def edit_field(state: OnboardingState, field_name: str, raw: str) -> str | None:
    """Apply an edit from the recap. Returns an error message, or None on success."""
    checks = {"user_name": v.check_person_name, "agent_name": v.check_agent_name, "help_topic": v.check_help_topic}
    if field_name not in checks:
        return "That can't be edited here."
    value, reason = checks[field_name](raw)
    if value is None:
        return {
            "user_name": "That doesn't look like a name. Just the name, please.",
            "agent_name": "That doesn't look like a name. Just the name, please.",
        }.get(field_name, "Could you say that a bit more specifically?")
    old = getattr(state, field_name)
    if old == value:
        return None
    setattr(state, field_name, value)
    if field_name == "agent_name":
        state.agent_name_defaulted = False
    if old and field_name not in state.corrected:
        state.corrected.append(field_name)
    label = EDITABLE_FIELDS[field_name]
    add_event_turn(state, f"Updated {label}: {value}")
    state.pending_notes.append(
        f"the user edited their {label} in the recap: {old!r} -> {value!r}. Use the new value; "
        "don't make a thing of it."
    )
    return None


def propose_event(state: OnboardingState, args: dict[str, Any]) -> ApplyResult:
    """The model can only propose; the event is written only when the user taps Add."""
    res = ApplyResult()
    if state.gmail_status != "connected":
        res.rejected.append("calendar isn't connected yet: offer to connect Gmail first (the button adds calendar access)")
        return res
    event, reason = v.check_event(args if isinstance(args, dict) else {}, state.user_tz)
    if event is None:
        res.rejected.append(f"event: {reason}")
        return res
    state.pending_event = event
    res.ui.append({"type": "confirm_event", "event": event})
    res.messages.append(
        f"proposed {event['title']!r} ({event['when']}). A confirm card is on their screen: ask them to tap Add. "
        "Don't say it's added; the app will tell you."
    )
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
    # Set the mood before the note is written, so this reply already adapts.
    # Frustration outranks rush; the model can still revise either via the tool.
    cue = v.mood_cue(text)
    if cue and not (cue == "rushed" and state.sentiment == "frustrated"):
        state.sentiment = cue
    # Deterministic guarantees for the two things users must never have to say twice.
    if v.wants_to_skip(text):
        state.skip_requested = True
    button_up = state.gmail_card_shown and state.gmail_status in ("not_connected", "popup_open")
    if (v.refuses_gmail(text) and state.gmail_status != "connected" and (state.gmail_card_shown or state.gmail_offer_count)) \
            or (button_up and v.puts_off(text)):
        state.gmail_status = "denied"
        state.gmail_card_shown = False


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

    if s.skip_requested and not s.graduated:
        return ["They asked to skip setup. Let them go right now: one warm line, set wants_to_skip=true. No questions."]

    if s.graduated:
        # Onboarding is over; the same brain is now their everyday assistant.
        lines = [
            "Onboarding is done: you're now their assistant. Help with whatever they ask, briefly and specifically. "
            "You can suggest, plan, and draft, but you can't send, book, or change anything yet: offer, and say "
            "you'll do it once they say go. Use the account snapshot when it's relevant. If there's a finding in "
            "'Things you noticed' they haven't heard yet and they seem open, you can bring it up with its fix."
        ]
        missing = [label for slot, label in (("user_name", "their name"), ("gmail", "Gmail")) if not getattr(s, slot)]
        if missing:
            lines.append(f"Still unknown: {', '.join(missing)}. Pick it up only if it comes up naturally; never as a checklist.")
        return lines

    if s.wrapping_up:
        if s.sentiment in ("rushed", "frustrated"):
            return [
                "Wrap-up, but they're in a hurry: one line with a starter idea or two (save starter_suggestions), "
                "no tips, no questions, and let them go now (set ready_to_start=true)."
            ]
        if not s.starter_suggestions:
            return [
                "Wrap-up. Give two or three concrete ways to get started, tailored to what they told you"
                + (" and their connected Gmail" if s.gmail_status == "connected" else "")
                + " (save as starter_suggestions), one or two quick tips on using Persona, then ask if they have "
                "any questions before they dive in. If they just said yes to jumping in, do all of that now."
            ]
        return [
            "Wrap-up, suggestions already given. Answer any question briefly and honestly, then check if there's "
            "anything else. When they're good, send them off in one warm line and set ready_to_start=true. "
            "Don't repeat the suggestions."
        ]

    if s.channel == "text" and not s.agent_name and not in_call(s):
        return [
            "Ask them to name you, the assistant (e.g. 'what do you want to name me?'; never 'what should I call you?'). "
            "If they're unsure, suggest two or three short, friendly names. "
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
    if grad_ok and rushed and gmail_still_offerable(s) and not s.gmail_card_shown:
        # Rushed isn't "no": one tap now is what makes the assistant useful from minute one.
        lines.append(
            "Offer to let them jump in now, and in the same breath the one-tap shortcut: connecting Gmail lets you "
            "start on what they need right away (the button is on screen now: set show_gmail_button=true). "
            "If they'd rather skip it or do it later, that's a no: set declined_gmail=true and don't bring it up again."
        )
        return lines
    if grad_ok and (rushed or frustrated):
        lines.append("Offer to let them jump in now.")
        return lines

    if s.gmail_status == "connected" and s.help_topic and not s.value_moment_done:
        if s.insights:
            lines.append(
                "Wow moment: from 'Things you noticed' below, lead with the most surprising, useful one: something "
                "they didn't ask about, ideally tied to what they need help with. Say it like a friend who just spotted "
                "it ('oh, heads up...'), then offer the fix; for a missing deadline or a prep block, offer to add it "
                "(call propose_calendar_event). One finding only, and mention a couple more are on their screen."
            )
            if not s.user_name and s.google_name:
                lines.append(
                    f"Also, their Google account says {s.google_name!r}: ask lightly whether that's what they go by."
                )
            return lines
        lines.append(
            "Value moment: from the account snapshot below, pick the ONE item most relevant to what they need "
            "help with and offer (don't do) one concrete next action, like drafting a reply or setting a reminder. "
            "Mention it the way a friend would ('I see you've got the dentist Thursday'), never read a subject "
            "line out word for word, and never mention anything private. If nothing fits or the snapshot is "
            "empty, base it on what they told you instead. Never invent events or emails."
        )
        if not s.user_name and s.google_name:
            lines.append(
                f"Also, their Google account says {s.google_name!r}: ask lightly whether that's what they go by or "
                "if they prefer something else, and save what they choose as user_name."
            )
        return lines

    discover = (
        "Discover what they need without asking for it. If they've already shared something about their life, "
        f"dig into that. Otherwise get curious about {discovery_angle(s)}, shaped as {discovery_move(s)}. "
        "Be specific to that angle, in your own words. No catch-all questions like 'what's been keeping you busy' "
        "or 'what's life like lately'."
    )
    name_passive = "If they mention their name, save it (the call opener asks who you're talking to)."
    confirm_google_name = (
        not s.user_name and s.google_name and s.gmail_status == "connected"
    )

    spoken_on_call = sum(1 for t in s.transcript if t.role == "user" and t.channel == "voice")
    early_name_ask = (
        "You don't know their name yet: fold a light ask into this reply while reacting to what they said "
        "(like '...who am I talking to, by the way?'), not as a standalone question."
    )

    if not s.help_topic:
        lines.append(discover)
        if not s.user_name:
            # Opener stays curious; if they didn't offer a name in their first answer, ask lightly next.
            # Their name comes first: the call opener asks it; if they didn't answer, ask once more lightly.
            lines.append(early_name_ask if spoken_on_call >= 1 or s.channel == "text" else name_passive)
    elif hunch_due(s):
        hunch = (
            "Show you get their life beyond what they said: name one non-obvious problem that usually comes with "
            "their situation, framed as a hunch ('I bet...' / 'let me guess...')."
        )
        if gmail_still_offerable(s) and not s.gmail_card_shown:
            # The hunch is the reason to connect: "want me to check if I'm right?"
            lines.append(
                hunch + " Then offer to check for real: if they connect Gmail you can look at their actual week "
                "right now (tell them the Connect Gmail button is on their screen: set show_gmail_button=true). "
                "Two or three short sentences; optional, never pushy."
            )
        else:
            lines.append(hunch + " Plus the concrete way you'd handle it. Two short sentences.")
    elif confirm_google_name:
        lines.append(
            f"Their Google account says {s.google_name!r}. Ask lightly whether that's what they go by or if they "
            "prefer something else, and save what they choose as user_name."
        )
    elif gmail_still_offerable(s) and not s.gmail_card_shown:
        lines.append(
            "Suggest connecting Gmail and tie it to what they need help with. Tell them the Connect Gmail button is "
            "on their screen now (set show_gmail_button=true). Make it clearly optional."
        )
    elif gmail_pending(s):
        lines.append(
            "The Connect Gmail button is on their screen and it's the next step. Keep chatting naturally; if they "
            "hesitate or ask, answer precisely what it can and can't see. Don't nag, and don't offer to skip it or "
            "to let them jump in yet: give them a moment with it. 'No', 'not now' and 'later' all mean no: respect it (declined_gmail=true) and drop it."
        )
    elif s.gmail_card_shown and s.gmail_status in ("not_connected", "popup_open"):
        lines.append("The Connect Gmail button is still on their screen. Don't push; keep chatting while they decide.")
        if grad_ok and not s.graduation_offered:
            lines.append("If it feels natural, offer to let them jump in; the button stays there if they change their mind.")
    elif not s.user_name:
        lines.append(
            "You still don't know what to call them. Fold a light ask into your reply (while reacting to something "
            "they said), not as a standalone question."
        )
    elif grad_ok and not s.graduation_offered:
        lines.append("Offer to let them jump in now, phrased as a question. Don't force it.")
    elif grad_ok and graduation_reoffer_ok(s):
        lines.append("Everything essential is done. Keep it brief; if they seem ready, you can offer again to jump in.")
    elif grad_ok:
        lines.append("You already offered to let them jump in; don't offer again yet. Just keep it brief and helpful.")

    if s.graduation_offered and not s.wrapping_up:
        # Written before the model reads their answer, so spell out what a yes means.
        lines.append(
            "If they're saying yes to jumping in (including 'I'm good' or 'sounds good'), that starts the wrap-up: "
            "set graduation_answer='accepted' and in this reply give starter ideas, a tip, and ask if they have "
            "questions. Don't say goodbye yet."
        )

    if s.turns_since_progress >= STALL_TURNS:
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
    elif s.wrapping_up:
        grad = "accepted; wrapping up"
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

    length = (
        "Spoken reply: one or two short sentences, 20 words max. React in a few words, then at most one question." if channel == "voice"
        else "Keep it short, like a text message."
    )
    if s.sentiment in ("rushed", "frustrated") or s.short_answer_streak >= 2:
        length = "One sentence."

    snapshot = ""
    if s.gmail_status == "connected" and s.account_snapshot and (value_moment_due(s) or s.wrapping_up or s.graduated):
        snapshot = "Account snapshot (read-only):\n" + describe_snapshot(s.account_snapshot, tz_name=s.user_tz)
        if s.insights:
            from .insights import describe_insights

            snapshot += "\nThings you noticed (found by the app in their calendar/inbox):\n" + describe_insights(s.insights)

    from datetime import datetime, timezone

    from .google import user_zone

    local_now = datetime.now(timezone.utc).astimezone(user_zone(s.user_tz))
    lines = [
        f"Now: {local_now.strftime('%A, %b %-d, %Y, %-I:%M%p').replace('AM', 'am').replace('PM', 'pm')} ({s.user_tz or 'UTC'})",
        f"Channel: {channel} ({call})",
        f"Filled: {filled}",
        f"Missing: {missing}",
        f"Gmail: {s.gmail_status}" + (" (button on screen)" if s.gmail_card_shown and s.gmail_status != "connected" else ""),
        f"Signals: {'; '.join(signals) if signals else 'none'}",
        f"Graduation: {grad}",
        *(
            [f"Waiting on them: the confirm card for {s.pending_event['title']!r} ({s.pending_event['when']}) is on screen."]
            if s.pending_event else []
        ),
        "Priority: " + " ".join(_priority(s)),
        "(Written before reading their latest message: if it already answers something above, don't ask it again.)",
        *([snapshot] if snapshot else []),
        length,
    ]
    return "<director_note>\n" + "\n".join(lines) + "\n</director_note>"
