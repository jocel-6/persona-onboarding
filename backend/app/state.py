"""Onboarding state: the slot table plus call/channel status and behavioral signals.

Code owns this object. The model only proposes changes through the save tool,
and the orchestrator validates them before they land here.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

GmailStatus = Literal["not_connected", "popup_open", "connected", "denied", "error"]
Channel = Literal["text", "voice"]
CallStatus = Literal[
    "not_started", "offered", "ringing", "in_progress", "hung_up", "declined", "missed", "completed"
]
Sentiment = Literal["neutral", "frustrated", "enthusiastic", "rushed", "chatty"]

SLOTS = ("agent_name", "user_name", "gmail", "help_topic")
DEFAULT_AGENT_NAME = "Nova"


class Turn(BaseModel):
    """One visible line of the shared transcript (voice and text alike)."""

    role: Literal["user", "agent", "event"]
    text: str
    channel: Channel
    ts: float = Field(default_factory=time.time)


class OnboardingState(BaseModel):
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)

    # The four slots.
    agent_name: str | None = None
    user_name: str | None = None
    gmail: str | None = None
    help_topic: str | None = None
    agent_name_defaulted: bool = False  # user skipped naming, so we used the default
    voice_id: str | None = None  # the voice they picked for their Persona (None = server default)
    phone: str | None = None  # #13: E.164 number, only if they asked Persona to call their phone

    gmail_status: GmailStatus = "not_connected"
    google_name: str | None = None  # from Google sign-in; confirmed with the user before it becomes user_name
    gmail_demo: bool = False
    # How the last Google sign-in popup ended: ok | denied | missing_scopes | error | expired.
    # The browser polls this, since Google's pages can cut the popup's link back to the app.
    google_result: str | None = None  # connected to the clearly labeled demo data, not a real account
    # Read-only snapshot fetched once at connect: upcoming events + inbox subjects,
    # private items already removed. Server-side only (never sent to the browser).
    account_snapshot: dict[str, Any] | None = None
    gmail_card_shown: bool = False
    gmail_offer_count: int = 0
    gmail_offered_at_turn: int | None = None  # user_turns when the button went up (don't offer to skip right away)

    channel: Channel = "text"
    call_status: CallStatus = "not_started"

    sentiment: Sentiment = "neutral"
    short_answer_streak: int = 0
    turns_since_progress: int = 0
    user_turns: int = 0

    value_moment_done: bool = False

    # Calendar: an event the agent proposed, waiting for the user's tap on the confirm card,
    # and the ones they confirmed. Nothing is written to their calendar without that tap.
    # "Persona noticed": problems found in their calendar/inbox by app/insights.py, best first.
    insights: list[dict[str, Any]] = Field(default_factory=list)
    deep_insights: list[dict[str, Any]] = Field(default_factory=list)  # #6, kept across recomputes
    hunch_done: bool = False
    week_summary: dict[str, Any] | None = None  # counts only, for the living profile
    tomorrow: dict[str, Any] | None = None  # "tomorrow at a glance" card (their own events, shown to them)  # offered a non-obvious "I bet..." problem from what they said
    pending_event: dict[str, Any] | None = None
    # #8: a reply Persona drafted, waiting for their "Save to Gmail drafts" tap; never sent.
    pending_draft: dict[str, Any] | None = None
    saved_drafts: list[dict[str, Any]] = Field(default_factory=list)
    added_events: list[dict[str, Any]] = Field(default_factory=list)

    # Wrap-up: after they accept graduation, tailored starters + tips + "any questions?"
    wrapping_up: bool = False
    starter_suggestions: list[str] = Field(default_factory=list)

    skip_requested: bool = False  # code heard a clear "skip the setup"; graduation follows this turn

    graduation_offered: bool = False
    graduation_offered_at_turn: int | None = None
    graduation_declined_at_turn: int | None = None
    graduated: bool = False

    # Slots that were corrected at least once, so the recap can show them.
    corrected: list[str] = Field(default_factory=list)

    # Shared transcript for the UI and the recap. Survives hangups and channel switches.
    transcript: list[Turn] = Field(default_factory=list)

    # Raw Messages API history (content blocks as dicts). Append-only so the
    # prompt cache prefix stays valid turn to turn.
    messages: list[dict[str, Any]] = Field(default_factory=list)

    # tool_result blocks owed to the model, sent at the start of the next user message.
    pending_tool_results: list[dict[str, Any]] = Field(default_factory=list)
    # Things that happened outside the conversation (e.g. an edit in the recap) that the
    # model should hear about on its next turn.
    pending_notes: list[str] = Field(default_factory=list)

    # The user's IANA timezone from the browser, so "tomorrow 9:15am" is right wherever the server runs.
    user_tz: str | None = None
    # What the code told the model on the latest turn, shown in the "Under the hood" panel.
    last_director_note: str | None = None

    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    # ---- derived views -------------------------------------------------

    def filled(self) -> dict[str, str]:
        return {s: getattr(self, s) for s in SLOTS if getattr(self, s)}

    def missing(self) -> list[str]:
        return [s for s in SLOTS if not getattr(self, s)]

    def public_view(self) -> dict[str, Any]:
        """What the browser is allowed to see: no raw API history, no tokens."""
        view = self.model_dump(exclude={"messages", "pending_tool_results", "pending_notes", "account_snapshot"})
        if view.get("pending_draft"):  # addresses and thread ids stay server-side
            view["pending_draft"] = {k: view["pending_draft"].get(k) for k in ("to_name", "subject", "body")}
        return view
