"""HTTP API for the onboarding agent.

  POST   /api/sessions                  start a session (scripted opener, no model call)
  GET    /api/sessions/{id}             resume: state + transcript
  POST   /api/sessions/{id}/messages    user typed or spoke something -> SSE stream
  POST   /api/sessions/{id}/events      app event (call, hangup, Gmail, resume) -> SSE stream
  DELETE /api/sessions/{id}             forget the session

Stream events: {"type": "delta", "text"} while the agent talks, {"type": "ui", "ui": {...}}
for screen changes, then {"type": "state", "state"} and {"type": "end"}.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import orchestrator as orch
from .brain import Brain
from .config import Settings
from .state import DEFAULT_AGENT_NAME, OnboardingState, Turn
from .store import SessionStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = Settings()
store = SessionStore(settings.db_path)
brain = Brain(settings)
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

app = FastAPI(title="Persona onboarding")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_methods=["*"],
    allow_headers=["*"],
)

NAME_POOL = ["Nova", "Juno", "Milo", "Kai", "Remy", "Wren", "Ollie", "Sage", "Pip", "Iris"]
OPENERS = [
    "Hey! I'm your new Persona. First things first: what do you want to call me?",
    "Hi there! Before anything else, I need a name. What should you call me?",
    "Hey, nice to meet you! Let's start with the fun part: what do you want to call me?",
]


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class EventIn(BaseModel):
    type: Literal[
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
        "graduate",
        "resumed",
    ]
    data: dict[str, Any] = Field(default_factory=dict)


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _load(session_id: str) -> OnboardingState:
    state = store.get(session_id)
    if state is None:
        raise HTTPException(404, "session not found")
    return state


def _sse_response(gen: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        gen, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


async def _stream_turn(
    session_id: str,
    *,
    user_text: str | None = None,
    event_text: str | None = None,
    pre: list[dict[str, Any]] | None = None,
    mutate=None,
) -> AsyncIterator[str]:
    """Serialize turns per session, run one, persist, and stream it out."""
    async with _locks[session_id]:
        state = _load(session_id)
        if mutate is not None:
            event_text = mutate(state)
        for ev in pre or []:
            yield _sse({"type": "ui", "ui": ev})
        if user_text is not None or event_text:
            async for ev in brain.run_turn(state, user_text=user_text, event_text=event_text):
                yield _sse(ev)
        store.save(state)
        yield _sse({"type": "state", "state": state.public_view()})
        yield _sse({"type": "end"})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "model": settings.llm_model, "gmail_stub": settings.gmail_stub}


@app.post("/api/sessions")
def create_session() -> dict[str, Any]:
    state = OnboardingState()
    opener = random.choice(OPENERS)
    names = random.sample(NAME_POOL, 3)
    # Seed the history so the model knows what it already said. Static text,
    # so it doesn't hurt caching.
    state.messages = [
        {"role": "user", "content": [{"type": "text", "text": "[event: the user opened Persona for the first time]"}]},
        {"role": "assistant", "content": [{"type": "text", "text": f"{opener} (A few ideas: {', '.join(names)}.)"}]},
    ]
    state.transcript.append(Turn(role="agent", text=opener, channel="text"))
    store.save(state)
    return {
        "state": state.public_view(),
        "ui": [{"type": "name_suggestions", "names": names}],
        "config": {"gmail_stub": settings.gmail_stub},
    }


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    return {"state": _load(session_id).public_view(), "config": {"gmail_stub": settings.gmail_stub}}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, bool]:
    store.delete(session_id)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/messages")
async def post_message(session_id: str, body: MessageIn) -> StreamingResponse:
    _load(session_id)
    return _sse_response(_stream_turn(session_id, user_text=body.text.strip()))


@app.post("/api/sessions/{session_id}/events")
async def post_event(session_id: str, body: EventIn) -> StreamingResponse:
    _load(session_id)
    pre: list[dict[str, Any]] = []

    def mutate(state: OnboardingState) -> str | None:
        """Apply the event to state; return event text for the model, or None for no reply."""
        t = body.type

        if t == "name_skipped":
            if state.agent_name:
                return None
            state.agent_name = DEFAULT_AGENT_NAME
            state.agent_name_defaulted = True
            orch.add_event_turn(state, f"Skipped naming. Going by {DEFAULT_AGENT_NAME}.")
            return (
                f"the user tapped 'skip' on naming you, so you're {DEFAULT_AGENT_NAME} for now (they can rename you "
                "anytime). Acknowledge in a few words and ask if they're up for a quick call or would rather text."
            )

        if t in ("call_accepted", "callback"):
            orch.start_call(state)
            pre.append({"type": "ringing"})
            return None

        if t == "call_declined":
            if state.call_status in ("ringing", "offered", "not_started"):
                state.call_status = "declined"
                state.channel = "text"
                return "the user chose to keep texting instead of a call. No pushback; carry on here."
            return None

        if t == "call_connected":
            state.call_status = "in_progress"
            state.channel = "voice"
            orch.add_event_turn(state, "Call connected")
            if state.user_turns > 1 and state.filled():
                return (
                    "the call just connected again (a callback). Pick up exactly where you left off; don't restart "
                    "or re-ask anything you already know."
                )
            return (
                "the call just connected and the user picked up. Introduce yourself by name and open with one "
                "curious, specific question as the director's note suggests. Don't ask for their name; they'll "
                "usually offer it."
            )

        if t == "call_missed":
            orch.end_call(state, "missed")
            orch.add_event_turn(state, "Missed call")
            pre.append({"type": "show_callback"})
            return (
                "you called but the user didn't pick up. Text them something like 'Missed you! No worries, we can do "
                "it here.' and mention they can tap to call back anytime. Then carry on over text."
            )

        if t == "hangup":
            if state.call_status != "in_progress":
                return None
            orch.end_call(state, "hangup")
            orch.add_event_turn(state, "Call ended")
            pre.append({"type": "show_callback"})
            return (
                "the call ended suddenly (the user hung up or it dropped) mid-conversation. You're now texting. Say "
                "something like 'Looks like we got cut off! Want me to call back, or finish up here?' in your own "
                "words, then continue from exactly where you stopped. Don't re-ask anything you already know."
            )

        if t == "gmail_popup_opened":
            state.gmail_status = "popup_open"
            return None

        if t == "gmail_connected":
            email = str(body.data.get("email") or "").strip()
            google_name = str(body.data.get("name") or "").strip().split(" ")[0]
            if google_name:
                state.google_name = google_name
            state.gmail_status = "connected"
            state.gmail = email or "connected"
            state.gmail_card_shown = False
            state.turns_since_progress = 0
            orch.add_event_turn(state, "Gmail connected")
            pre.append({"type": "hide_gmail_card"})
            return "the user just connected Gmail. Confirm it warmly in a few words, then continue."

        if t in ("gmail_closed", "gmail_denied"):
            state.gmail_status = "not_connected" if t == "gmail_closed" else "denied"
            state.gmail_card_shown = False
            pre.append({"type": "hide_gmail_card"})
            return (
                "the user closed the Google sign-in without connecting. No guilt: say they can connect it later, and "
                "keep going."
            )

        if t == "graduate":
            orch.graduate(state)
            pre.append({"type": "graduated"})
            return "the user tapped the button to start using Persona. Say a warm, one-line send-off."

        if t == "resumed":
            if state.graduated or not any(turn.role == "user" for turn in state.transcript):
                return None
            if state.call_status in ("in_progress", "ringing"):
                orch.end_call(state, "hangup")
                pre.append({"type": "show_callback"})
            return (
                "the user left and just came back (page refresh). Welcome them back briefly and remind them what you "
                "were just talking about, then continue. Don't re-ask anything you already know."
            )
        return None

    return _sse_response(_stream_turn(session_id, mutate=mutate, pre=pre))
