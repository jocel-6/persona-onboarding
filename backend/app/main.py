"""HTTP API for the onboarding agent.

  POST   /api/sessions                  start a session (scripted opener, no model call)
  GET    /api/sessions/{id}             resume: state + transcript
  POST   /api/sessions/{id}/messages    user typed something -> SSE stream
  POST   /api/sessions/{id}/events      app event (call, hangup, Gmail, resume) -> SSE stream
  DELETE /api/sessions/{id}             forget the session
  POST   /api/offer, PATCH /api/offer   WebRTC signaling for the voice call

Stream events: {"type": "delta", "text"} while the agent talks, {"type": "ui", "ui": {...}}
for screen changes, then {"type": "state", "state"} and {"type": "end"}. During a
voice call the same messages arrive over the WebRTC data channel instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import runtime
from .events import EventType, apply_event
from .state import OnboardingState, Turn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("persona.api")

settings = runtime.settings
store = runtime.store
brain = runtime.brain

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

# Events that, during a live voice call, should be spoken on the call rather than texted.
SPOKEN_DURING_CALL = {"gmail_connected", "gmail_closed", "gmail_denied", "gmail_popup_opened", "graduate"}


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class EventIn(BaseModel):
    type: EventType
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


def _config() -> dict[str, Any]:
    problem = settings.voice_problem()
    return {"gmail_stub": settings.gmail_stub, "voice": problem is None, "voice_problem": problem}


async def _stream_turn(
    session_id: str,
    *,
    user_text: str | None = None,
    event: EventIn | None = None,
) -> AsyncIterator[str]:
    """Serialize turns per session, run one, persist, and stream it out."""
    spoken_on: Any = None
    async with runtime.locks[session_id]:
        state = _load(session_id)
        event_text = None
        if event is not None:
            event_text, ui = apply_event(state, event.type, event.data)
            for ev in ui:
                yield _sse({"type": "ui", "ui": ev})
            if event.type in SPOKEN_DURING_CALL and state.call_status == "in_progress":
                from .voice.call import active_calls

                spoken_on = active_calls.get(session_id)
        if spoken_on is None and (user_text is not None or event_text):
            async for ev in brain.run_turn(state, user_text=user_text, event_text=event_text):
                yield _sse(ev)
        store.save(state)
        yield _sse({"type": "state", "state": state.public_view()})
        yield _sse({"type": "end"})
    if spoken_on is not None and event_text:
        await spoken_on.say_event(event_text)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "model": settings.llm_model, **_config()}


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
    return {"state": state.public_view(), "ui": [{"type": "name_suggestions", "names": names}], "config": _config()}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    return {"state": _load(session_id).public_view(), "config": _config()}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, bool]:
    from .voice.call import end_call

    await end_call(session_id)
    store.delete(session_id)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/messages")
async def post_message(session_id: str, body: MessageIn) -> StreamingResponse:
    _load(session_id)
    return _sse_response(_stream_turn(session_id, user_text=body.text.strip()))


@app.post("/api/sessions/{session_id}/events")
async def post_event(session_id: str, body: EventIn) -> StreamingResponse:
    _load(session_id)
    if body.type == "hangup":
        # Stop the audio pipeline first so a half-spoken turn can't race the text follow-up.
        from .voice.call import end_call

        await end_call(session_id)
    return _sse_response(_stream_turn(session_id, event=body))


# ---------------------------------------------------------------------------
# Voice call signaling (WebRTC, browser <-> this server; no third-party room service)
# ---------------------------------------------------------------------------

_webrtc_handler: Any = None


def _handler():
    global _webrtc_handler
    if _webrtc_handler is None:
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler

        _webrtc_handler = SmallWebRTCRequestHandler()
    return _webrtc_handler


@app.post("/api/offer")
async def webrtc_offer(request: Request) -> dict[str, Any]:
    problem = settings.voice_problem()
    if problem:
        raise HTTPException(503, f"voice unavailable: {problem}")
    from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest

    from .voice.call import VoiceCall, end_call

    body = await request.json()
    req = SmallWebRTCRequest.from_dict(body)
    session_id = str((req.request_data or {}).get("session_id") or "")
    if not req.pc_id:
        state = _load(session_id)
        if state.call_status not in ("ringing", "in_progress"):
            raise HTTPException(409, f"no call to connect (call_status={state.call_status})")
        await end_call(session_id)  # one live call per session

    async def on_connection(connection):
        call = VoiceCall(session_id, connection)
        asyncio.create_task(call.run())

    answer = await _handler().handle_web_request(req, on_connection)
    return answer or {}


@app.patch("/api/offer")
async def webrtc_ice(request: Request) -> dict[str, bool]:
    from pipecat.transports.smallwebrtc.request_handler import IceCandidate, SmallWebRTCPatchRequest

    body = await request.json()
    patch = SmallWebRTCPatchRequest(
        pc_id=body["pc_id"],
        candidates=[
            IceCandidate(
                candidate=c["candidate"],
                sdp_mid=c.get("sdp_mid", c.get("sdpMid")),
                sdp_mline_index=c.get("sdp_mline_index", c.get("sdpMLineIndex")),
            )
            for c in body.get("candidates", [])
        ],
    )
    await _handler().handle_patch_request(patch)
    return {"ok": True}
