"""HTTP API for the onboarding agent.

  POST   /api/sessions                  start a session (scripted opener, no model call)
  GET    /api/sessions/{id}             resume: state + transcript
  POST   /api/sessions/{id}/messages    user typed something -> SSE stream
  POST   /api/sessions/{id}/events      app event (call, hangup, Gmail, resume) -> SSE stream
  DELETE /api/sessions/{id}             forget the session
  POST   /api/sessions/{id}/fields      fix a field from the recap (validated, no model call)
  POST   /api/offer, PATCH /api/offer   WebRTC signaling for the voice call
  GET    /api/google/start, /callback   Google sign-in popup (read-only calendar + email headers)

Stream events: {"type": "delta", "text"} while the agent talks, {"type": "ui", "ui": {...}}
for screen changes, then {"type": "state", "state"} and {"type": "end"}. During a
voice call the same messages arrive over the WebRTC data channel instead.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import google, runtime
from .events import EventType, apply_event
from .state import OnboardingState, Turn

_db = runtime.settings.db_path
LOG_FILE = (Path(_db).parent if _db != ":memory:" else Path(__file__).resolve().parent.parent / "data") / "server.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
    force=True,
)
try:  # Pipecat logs through loguru; keep a copy next to ours for debugging calls.
    from loguru import logger as _loguru

    _loguru.add(str(LOG_FILE), level="DEBUG", rotation="20 MB", retention=3, enqueue=True)
except ImportError:
    pass
log = logging.getLogger("persona.api")

settings = runtime.settings
store = runtime.store
brain = runtime.brain

@asynccontextmanager
async def lifespan(_app: FastAPI):
    sweeper = asyncio.create_task(_retention_loop())
    yield
    sweeper.cancel()


app = FastAPI(title="Persona onboarding", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins(),
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
SPOKEN_DURING_CALL = {
    "gmail_connected", "gmail_closed", "gmail_denied", "gmail_popup_opened", "graduate",
    "event_confirmed", "event_cancelled",
}


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class FieldEditIn(BaseModel):
    field: str
    value: str = Field(min_length=1, max_length=240)


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
    return {
        "gmail_stub": settings.gmail_stub,
        "gmail_mode": "stub" if settings.gmail_stub else "google",
        "demo_data": settings.allow_demo_data,
        "voice": problem is None,
        "voice_problem": problem,
        "ice_servers": settings.ice_servers(),
    }


async def _add_pending_event(session_id: str) -> str:
    """The user tapped Add: write the pending event to their calendar. Never called by the model."""
    import httpx

    state = _load(session_id)
    ev = state.pending_event
    if not ev:
        return "none"
    if state.gmail_demo or settings.gmail_stub:
        return "demo_added"
    tokens = store.get_tokens(session_id)
    if not tokens or not google.can_add_events(tokens):
        return "needs_permission"
    try:
        tokens = await google.fresh_access_token(settings.google_client_id, settings.google_client_secret, tokens)
        store.save_tokens(session_id, tokens)
        await google.insert_event(tokens["access_token"], ev)
        log.info("calendar event added for session %s", session_id[:8])
        return "added"
    except httpx.HTTPStatusError as e:
        log.warning("calendar insert failed for session %s: HTTP %s", session_id[:8], e.response.status_code)
        return "needs_permission" if e.response.status_code in (401, 403) else "error"
    except httpx.HTTPError:
        log.exception("calendar insert failed for session %s", session_id[:8])
        return "error"


async def _revoke_google(session_id: str) -> None:
    tokens = store.get_tokens(session_id)
    if tokens:
        await google.revoke(tokens.get("refresh_token") or tokens.get("access_token", ""))
        store.delete_tokens(session_id)


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
# Retention: nothing is kept forever
# ---------------------------------------------------------------------------


async def purge_stale_sessions() -> int:
    """Revoke Google access and delete sessions idle longer than RETENTION_DAYS."""
    stale = store.stale_session_ids(settings.retention_days * 86400)
    for sid in stale:
        await _revoke_google(sid)
        store.delete(sid)
    if stale:
        log.info("retention: purged %d idle session(s)", len(stale))
    return len(stale)


async def _retention_loop() -> None:
    while True:
        try:
            await purge_stale_sessions()
        except Exception:  # noqa: BLE001 - retention must never take the app down
            log.exception("retention sweep failed")
        await asyncio.sleep(3600)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "model": settings.llm_model, **_config()}


class SessionIn(BaseModel):
    tz: str | None = Field(default=None, max_length=64)


@app.post("/api/sessions")
def create_session(body: SessionIn | None = None) -> dict[str, Any]:
    state = OnboardingState(user_tz=(body.tz if body else None) or None)
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
    await _revoke_google(session_id)  # tokens are deleted when the session ends
    store.delete(session_id)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/fields")
async def edit_field(session_id: str, body: FieldEditIn) -> dict[str, Any]:
    from . import orchestrator as orch

    async with runtime.locks[session_id]:
        state = _load(session_id)
        error = orch.edit_field(state, body.field, body.value)
        if error:
            raise HTTPException(422, error)
        store.save(state)
        return {"state": state.public_view()}


@app.post("/api/sessions/{session_id}/messages")
async def post_message(session_id: str, body: MessageIn) -> StreamingResponse:
    _load(session_id)
    return _sse_response(_stream_turn(session_id, user_text=body.text.strip()))


@app.post("/api/sessions/{session_id}/events")
async def post_event(session_id: str, body: EventIn) -> StreamingResponse:
    _load(session_id)
    if body.type == "gmail_disconnected":
        await _revoke_google(session_id)
    if body.type == "event_confirmed":
        body.data["result"] = await _add_pending_event(session_id)
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
        from aiortc import RTCIceServer
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler

        _webrtc_handler = SmallWebRTCRequestHandler(
            ice_servers=[
                RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
                for s in settings.ice_servers()
            ]
        )
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


# ---------------------------------------------------------------------------
# Google sign-in (popup). Only this callback can mark Gmail as connected.
# ---------------------------------------------------------------------------

OAUTH_STATE_TTL = 600
_oauth_states: dict[str, tuple[str, float]] = {}


def _popup_result(payload: dict[str, Any]) -> HTMLResponse:
    """Tiny page that tells the app how sign-in went, then closes itself."""
    data = json.dumps({"source": "persona-google", **payload})
    origin = json.dumps(settings.frontend_origins()[0] if settings.frontend_origins() else "*")
    msg = "Connected! You can close this window." if payload.get("ok") else "Not connected. You can close this window."
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8"><title>Persona</title>
<body style="font:16px system-ui;padding:32px">{html.escape(msg)}
<script>try{{window.opener&&window.opener.postMessage({data},{origin})}}catch(e){{}}setTimeout(()=>window.close(),300)</script>"""
    )


@app.get("/api/google/start")
async def google_start(session_id: str):
    if settings.gmail_stub:
        raise HTTPException(400, "Google sign-in isn't configured (GMAIL_STUB mode)")
    _load(session_id)
    now = time.time()
    for k, (_, t) in list(_oauth_states.items()):
        if now - t > OAUTH_STATE_TTL:
            _oauth_states.pop(k, None)
    token = google.new_state_token()
    _oauth_states[token] = (session_id, now)
    return RedirectResponse(google.auth_url(settings.google_client_id, settings.google_redirect_uri, token))


async def _record_google_result(session_id: str, result: str) -> None:
    async with runtime.locks[session_id]:
        st = store.get(session_id)
        if st is not None:
            st.google_result = result
            store.save(st)


@app.get("/api/google/callback")
async def google_callback(state: str = "", code: str = "", error: str = ""):
    entry = _oauth_states.pop(state, None)
    if entry is None or time.time() - entry[1] > OAUTH_STATE_TTL:
        return _popup_result({"ok": False, "reason": "expired"})
    session_id = entry[0]
    if error:
        reason = "denied" if error == "access_denied" else "error"
        await _record_google_result(session_id, reason)
        return _popup_result({"ok": False, "reason": reason})
    try:
        tokens = await google.exchange_code(
            settings.google_client_id, settings.google_client_secret, settings.google_redirect_uri, code
        )
        if google.granted_all_scopes(tokens):
            await google.revoke(tokens.get("access_token", ""))
            await _record_google_result(session_id, "missing_scopes")
            return _popup_result({"ok": False, "reason": "missing_scopes"})
        profile = await google.fetch_profile(tokens["access_token"])
        snapshot = await google.fetch_snapshot(tokens["access_token"])
    except Exception:  # noqa: BLE001 - any failure here just means "not connected"; never surface details
        log.exception("Google sign-in failed for session %s", session_id[:8])
        await _record_google_result(session_id, "error")
        return _popup_result({"ok": False, "reason": "error"})

    store.save_tokens(session_id, tokens)
    async with runtime.locks[session_id]:
        st = store.get(session_id)
        if st is None:
            return _popup_result({"ok": False, "reason": "expired"})
        st.gmail = profile.get("email") or "connected"
        st.google_name = (profile.get("given_name") or profile.get("name", "").split(" ")[0]) or None
        st.account_snapshot = snapshot
        st.gmail_demo = False
        st.gmail_status = "connected"
        st.google_result = "ok"
        store.save(st)
    log.info(
        "Google connected for session %s: %d events, %d subjects",
        session_id[:8], len(snapshot["events"]), len(snapshot["emails"]),
    )
    return _popup_result({"ok": True})
