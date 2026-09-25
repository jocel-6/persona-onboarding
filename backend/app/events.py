"""App events: things that happen outside the conversation (call, hangup, Gmail, resume).

Shared by the HTTP events endpoint and the voice pipeline, so an event means the
same thing whether the user is texting or on a call.
"""

from __future__ import annotations

from typing import Any, Literal

from . import orchestrator as orch
from .state import DEFAULT_AGENT_NAME, OnboardingState

EventType = Literal[
    "name_skipped",
    "call_accepted",
    "call_declined",
    "call_connected",
    "call_missed",
    "hangup",
    "callback",
    "gmail_popup_opened",
    "gmail_connected",
    "gmail_closed",
    "gmail_denied",
    "gmail_disconnected",
    "graduate",
    "resumed",
]


def apply_event(state: OnboardingState, t: str, data: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    """Apply an event to state.

    Returns (event text for the model, or None when no reply is needed; UI events
    to send before the reply).
    """
    ui: list[dict[str, Any]] = []

    if t == "name_skipped":
        if state.agent_name:
            return None, ui
        state.agent_name = DEFAULT_AGENT_NAME
        state.agent_name_defaulted = True
        orch.add_event_turn(state, f"Skipped naming. Going by {DEFAULT_AGENT_NAME}.")
        return (
            f"the user tapped 'skip' on naming you, so you're {DEFAULT_AGENT_NAME} for now (they can rename you "
            "anytime). Acknowledge in a few words and ask if they're up for a quick call or would rather text."
        ), ui

    if t in ("call_accepted", "callback"):
        orch.start_call(state)
        ui.append({"type": "ringing"})
        return None, ui

    if t == "call_declined":
        if state.call_status in ("ringing", "offered", "not_started"):
            state.call_status = "declined"
            state.channel = "text"
            return "the user chose to keep texting instead of a call. No pushback; carry on here.", ui
        return None, ui

    if t == "call_connected":
        if state.call_status not in ("ringing", "in_progress"):
            return None, ui  # the call already ended (or never rang): don't revive it
        state.call_status = "in_progress"
        state.channel = "voice"
        orch.add_event_turn(state, "Call connected")
        if state.user_turns > 1 and state.filled():
            return (
                "the call just connected again (a callback). Pick up exactly where you left off; don't restart "
                "or re-ask anything you already know."
            ), ui
        return (
            "the call just connected and the user picked up. Introduce yourself by name and open with one "
            "curious, specific question as the director's note suggests. Don't ask for their name; they'll "
            "usually offer it."
        ), ui

    if t == "call_missed":
        orch.end_call(state, "missed")
        orch.add_event_turn(state, "Missed call")
        ui.append({"type": "show_callback"})
        return (
            "you called but the user didn't pick up. Text them something like 'Missed you! No worries, we can do "
            "it here.' and mention they can tap to call back anytime. Then carry on over text."
        ), ui

    if t == "hangup":
        if state.call_status != "in_progress":
            return None, ui
        orch.end_call(state, "hangup")
        orch.add_event_turn(state, "Call ended")
        ui.append({"type": "show_callback"})
        return (
            "the call ended suddenly (the user hung up or it dropped) mid-conversation. You're now texting. Say "
            "something like 'Looks like we got cut off! Want me to call back, or finish up here?' in your own "
            "words, then continue from exactly where you stopped. Don't re-ask anything you already know."
        ), ui

    if t == "gmail_popup_opened":
        if state.gmail_status != "connected":
            state.gmail_status = "popup_open"
        state.google_result = None
        return None, ui

    if t == "gmail_connected":
        from . import runtime
        from .google import demo_snapshot

        if data.get("demo"):
            if not runtime.settings.allow_demo_data:
                return None, ui
            state.account_snapshot = demo_snapshot(tz_name=state.user_tz)
            state.gmail_demo = True
            state.gmail = "demo account"
        elif runtime.settings.gmail_stub:
            email = str(data.get("email") or "").strip()
            google_name = str(data.get("name") or "").strip().split(" ")[0]
            if google_name:
                state.google_name = google_name
            state.gmail = email or "connected"
        elif state.gmail_status != "connected" or not runtime.store.get_tokens(state.session_id):
            # Real mode: only the OAuth callback can connect Gmail; the browser can't claim it.
            return None, ui
        state.gmail_status = "connected"
        state.gmail_card_shown = False
        state.value_moment_done = False
        state.turns_since_progress = 0
        orch.add_event_turn(state, "Demo data connected" if state.gmail_demo else "Gmail connected")
        ui.append({"type": "hide_gmail_card"})
        if state.gmail_demo:
            return (
                "the user chose the demo account instead of signing in with Google. Confirm in a few words that "
                "you're using demo data, then continue."
            ), ui
        return "the user just connected Gmail. Confirm it warmly in a few words, then continue.", ui

    if t == "gmail_disconnected":
        state.gmail_status = "not_connected"
        state.gmail = None
        state.gmail_demo = False
        state.account_snapshot = None
        orch.add_event_turn(state, "Gmail disconnected")
        return "the user disconnected Gmail. Acknowledge in a few words, no guilt, and carry on.", ui

    if t in ("gmail_closed", "gmail_denied"):
        from . import runtime

        state.gmail_status = "not_connected" if t == "gmail_closed" else "denied"
        state.gmail_card_shown = False
        ui.append({"type": "hide_gmail_card"})
        reason = str(data.get("reason") or "")
        if reason == "missing_scopes":
            return (
                "the user signed in with Google but unticked the calendar or email permissions, so nothing was "
                "connected. No guilt: say that's totally fine, it can be connected later, and keep going."
            ), ui
        hint = ""
        if not runtime.settings.gmail_stub and t == "gmail_closed":
            hint = (
                " If it seems relevant, mention in one short line that Google blocks accounts that aren't on this "
                "test app's list, and that they can use the demo account or skip for now."
            )
        return (
            "the user closed the Google sign-in without connecting. No guilt: say they can connect it later, and "
            "keep going." + hint
        ), ui

    if t == "graduate":
        orch.graduate(state)
        ui.append({"type": "graduated"})
        return "the user tapped the button to start using Persona. Say a warm, one-line send-off.", ui

    if t == "resumed":
        if state.graduated or not any(turn.role == "user" for turn in state.transcript):
            return None, ui
        if state.call_status in ("in_progress", "ringing"):
            orch.end_call(state, "hangup")
            ui.append({"type": "show_callback"})
        return (
            "the user left and just came back (page refresh). Welcome them back briefly and remind them what you "
            "were just talking about, then continue. Don't re-ask anything you already know."
        ), ui

    return None, ui
