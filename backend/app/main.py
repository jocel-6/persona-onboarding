"""HTTP API for the onboarding agent.

  POST   /api/sessions                  start a session (scripted opener, no model call)
  GET    /api/sessions/{id}             resume: state + transcript
  POST   /api/sessions/{id}/messages    user typed something -> SSE stream
  POST   /api/sessions/{id}/events      app event (call, hangup, Gmail, resume) -> SSE stream
  DELETE /api/sessions/{id}             forget the session
  POST   /api/sessions/{id}/fields      fix a field from the recap (validated, no model call)
  GET    /api/sessions/{id}/export      everything Persona stored about you, as JSON (no tokens)
  GET    /api/metrics                   funnel, latency and cost for the dashboard (no content)
  GET    /api/voices/{voice_id}/sample  "Hi, I'm <name>!" in that voice, for the picker
  POST   /api/sessions/{id}/voice       pick the agent's voice
  POST   /api/sessions/{id}/insights/{insight_id}/add   turn a finding's fix into a confirm card
  POST   /api/sessions/{id}/insights/{insight_id}/draft draft a reply (shown for editing; saved only on a tap)
  POST   /api/offer, PATCH /api/offer   WebRTC signaling for the voice call
  POST   /api/sessions/{id}/phone-call  #13: Persona calls your real phone (Twilio)
  WS     /api/twilio/stream             the phone call's audio, into the same pipeline
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
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
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

# The first screen is the call: nothing is asked in text. The call collects their name, what they
# need and Gmail; naming the assistant is an optional tap on screen (the brief: everything but the
# agent's name on the call). Typing instead of calling works too.
OPENERS = [
    "Hey! I'm your new Persona. Tap Start call and let's talk, it takes two minutes. Or just type here if you'd rather text.",
    "Hi there, I'm your new Persona! Start a quick call and we'll get you set up in two minutes. Prefer texting? Just type.",
]

# Events that, during a live voice call, should be spoken on the call rather than texted.
SPOKEN_DURING_CALL = {
    "gmail_connected", "gmail_closed", "gmail_denied", "gmail_popup_opened", "graduate",
    "agent_named", "name_skipped",
    "event_confirmed", "event_cancelled", "draft_confirmed", "draft_cancelled",
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


def record_turn_metrics(session_id: str, channel: str, done: dict[str, Any]) -> None:
    from .metrics import turn_cost

    u = done.get("usage") or {}
    store.record_turn(
        session_id=session_id, channel=channel, model=settings.llm_model,
        ttft_ms=done["latency"].get("ttft_ms"), total_ms=done["latency"].get("total_ms"),
        input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
        cache_read_tokens=u.get("cache_read_input_tokens"), cache_write_tokens=u.get("cache_creation_input_tokens"),
        cost_usd=turn_cost(settings.llm_model, u),
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
        "voice_choices": [{k: v.get(k) for k in ("id", "name", "vibe")} for v in settings.voice_choices()],
        "phone": settings.phone_problem() is None,
    }


async def _deep_insights_later(session_id: str) -> None:
    """#6: the deep look runs beside the conversation, never in its way."""
    from datetime import datetime, timezone

    from . import orchestrator as orch
    from .deep_insights import find_deep_insights
    from .google import describe_snapshot, user_zone
    from .insights import describe_insights

    state = store.get(session_id)
    if state is None or not state.account_snapshot or state.deep_insights:
        log.info("deep insights skipped for session %s (no snapshot yet, or already done)", session_id[:8])
        return
    now = datetime.now(timezone.utc).astimezone(user_zone(state.user_tz))
    found = await find_deep_insights(
        brain.client,
        settings.deep_insights_model,
        snapshot_text=describe_snapshot(state.account_snapshot, tz_name=state.user_tz),
        rule_findings=describe_insights(state.insights),
        help_topic=state.help_topic,
        user_name=state.user_name,
        now_text=now.strftime("%A, %b %-d, %Y, %-I:%M%p"),
    )
    if not found:
        return
    async with runtime.locks[session_id]:
        state = store.get(session_id)
        if state is None or state.gmail_status != "connected":
            return
        state.deep_insights = found
        orch.refresh_insights(state)
        store.save(state)
    from .voice.call import active_calls

    if call := active_calls.get(session_id):
        await call.brain_svc._send({"type": "state", "state": state.public_view()})


async def _save_pending_draft(session_id: str, edited_body: str) -> str:
    """The user tapped Save: put the (possibly edited) reply in their Gmail drafts. Never sends."""
    import httpx

    async with runtime.locks[session_id]:
        state = _load(session_id)
        d = state.pending_draft
        if not d:
            return "none"
        if edited_body.strip():
            d["body"] = edited_body.strip()[:4000]
            store.save(state)
    if state.gmail_demo or settings.gmail_stub or not d.get("to_email"):
        return "demo_saved"
    tokens = store.get_tokens(session_id)
    if not tokens or not google.can_create_drafts(tokens):
        return "needs_permission"
    try:
        tokens = await google.fresh_access_token(settings.google_client_id, settings.google_client_secret, tokens)
        store.save_tokens(session_id, tokens)
        await google.create_draft(tokens["access_token"], d)
        log.info("draft saved for session %s", session_id[:8])
        return "saved"
    except httpx.HTTPStatusError as e:
        log.warning("draft save failed for session %s: HTTP %s", session_id[:8], e.response.status_code)
        return "needs_permission" if e.response.status_code in (401, 403) else "error"
    except httpx.HTTPError:
        log.exception("draft save failed for session %s", session_id[:8])
        return "error"


async def _add_pending_event(session_id: str) -> str:
    """The user tapped Add: write the pending event to their calendar. Never called by the model."""
    import httpx

    state = _load(session_id)
    ev = state.pending_event
    if not ev:
        return "none"
    moving = ev.get("op") == "move"
    if state.gmail_demo or settings.gmail_stub:
        return "demo_moved" if moving else "demo_added"
    tokens = store.get_tokens(session_id)
    if not tokens or not google.can_add_events(tokens):
        return "needs_permission"
    try:
        tokens = await google.fresh_access_token(settings.google_client_id, settings.google_client_secret, tokens)
        store.save_tokens(session_id, tokens)
        if moving:
            await google.move_event(tokens["access_token"], ev["event_id"], ev["start"], ev["end"], ev["tz"])
            log.info("calendar event moved for session %s", session_id[:8])
            return "moved"
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
                if ev["type"] == "done":
                    record_turn_metrics(session_id, state.channel, ev)
                yield _sse(ev)
        store.save(state)
        yield _sse({"type": "state", "state": state.public_view()})
        yield _sse({"type": "end"})
        # #6: start the deep look only once the snapshot is saved (it used to start before, find
        # nothing, and quietly give up).
        if event is not None and event.type == "gmail_connected" and settings.deep_insights \
                and state.gmail_status == "connected" and state.account_snapshot and not state.deep_insights:
            asyncio.create_task(_deep_insights_later(session_id))
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
    # Seed the history so the model knows what it already said. Static text,
    # so it doesn't hurt caching.
    state.messages = [
        {"role": "user", "content": [{"type": "text", "text": "[event: the user opened Persona for the first time]"}]},
        {"role": "assistant", "content": [{"type": "text", "text": opener}]},
    ]
    state.transcript.append(Turn(role="agent", text=opener, channel="text"))
    state.call_status = "offered"
    store.save(state)
    return {"state": state.public_view(), "ui": [{"type": "show_call_offer"}], "config": _config()}


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


@app.post("/api/sessions/{session_id}/insights/{insight_id}/add")
async def insight_add(session_id: str, insight_id: str) -> dict[str, Any]:
    """One tap on a finding's fix. Still only proposes: the confirm card's Add does the write."""
    from . import validation

    async with runtime.locks[session_id]:
        state = _load(session_id)
        ins = next((i for i in state.insights if i.get("id") == insight_id), None)
        action = (ins or {}).get("action") or {}
        if action.get("type") not in ("add_event", "move_event"):
            raise HTTPException(404, "nothing to add for that finding")
        event, reason = validation.check_event(action["event"], state.user_tz)
        if event is None:
            raise HTTPException(422, reason)
        if action["type"] == "move_event":
            event = {**event, "op": "move", "event_id": action["event"]["event_id"]}
        state.pending_event = event
        store.save(state)
        return {"state": state.public_view()}


class VoiceIn(BaseModel):
    voice_id: str = Field(min_length=1, max_length=64)


@app.get("/api/voices/{voice_id}/sample")
async def voice_sample(voice_id: str, name: str = "Nova") -> Response:
    import httpx

    from . import validation
    from .voice.samples import sample

    if voice_id not in {v["id"] for v in settings.voice_choices()}:
        raise HTTPException(404, "unknown voice")
    clean, _ = validation.check_agent_name(name)
    try:
        audio = await sample(settings, voice_id, clean or "Nova")
    except httpx.HTTPError:
        raise HTTPException(502, "couldn't make a sample right now")
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/sessions/{session_id}/voice")
async def pick_voice(session_id: str, body: VoiceIn) -> dict[str, Any]:
    choices = {v["id"]: v for v in settings.voice_choices()}
    if body.voice_id not in choices:
        raise HTTPException(422, "unknown voice")
    async with runtime.locks[session_id]:
        state = _load(session_id)
        state.voice_id = body.voice_id
        from . import orchestrator as orch

        orch.add_event_turn(state, f"Voice: {choices[body.voice_id].get('name', 'picked')}")
        store.save(state)
        return {"state": state.public_view()}


@app.post("/api/sessions/{session_id}/insights/{insight_id}/draft")
async def insight_draft(session_id: str, insight_id: str) -> dict[str, Any]:
    """#8: write a reply for someone waiting on the user. Shown for editing; nothing is saved yet."""
    from .drafts import write_draft

    state = _load(session_id)
    ins = next((i for i in state.insights if i.get("id") == insight_id), None)
    action = (ins or {}).get("action") or {}
    emails = (state.account_snapshot or {}).get("emails", [])
    idx = action.get("email_index")
    if action.get("type") != "draft_reply" or not isinstance(idx, int) or not 0 <= idx < len(emails):
        raise HTTPException(404, "nothing to reply to for that finding")
    email = emails[idx]
    subject = email["subject"]
    try:
        text = await write_draft(
            brain.client, settings.llm_model, to_name=email.get("from") or "there", subject=subject,
            user_name=state.user_name, suggested_time=action.get("suggested_time"),
        )
    except Exception:  # noqa: BLE001 - a failed draft is just "try again", never an error dump
        log.exception("draft failed for session %s", session_id[:8])
        raise HTTPException(502, "couldn't write a draft right now")
    async with runtime.locks[session_id]:
        state = _load(session_id)
        state.pending_draft = {
            "to_name": email.get("from") or "",
            "to_email": email.get("from_email"),
            "subject": subject if subject.lower().startswith("re:") else f"Re: {subject}",
            "body": text,
            "thread_id": email.get("thread_id"),
            "in_reply_to": email.get("message_id"),
        }
        store.save(state)
        return {"state": state.public_view()}


@app.get("/api/sessions/{session_id}/export")
def export_session(session_id: str) -> Response:
    """#14: everything Persona stored about you, in one file. Tokens are never included."""
    state = _load(session_id)
    data = {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "about_you": {k: getattr(state, k) for k in (
            "agent_name", "user_name", "help_topic", "voice_id", "user_tz", "gmail", "gmail_status",
        )},
        "what_it_read": state.account_snapshot,
        "what_it_noticed": state.insights,
        "what_it_did_for_you": {"calendar": state.added_events, "drafts": state.saved_drafts},
        "conversation": [t.model_dump() for t in state.transcript],
        "retention": f"Deleted after {settings.retention_days:g} idle days; Google access is revoked first.",
        "google_access_token_stored": bool(store.get_tokens(session_id)),
    }
    return Response(
        json.dumps(data, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="persona-my-data.json"'},
    )


class FeedbackIn(BaseModel):
    rating: Literal["up", "down"] | None = None
    text: str | None = Field(default=None, max_length=1000)


@app.post("/api/sessions/{session_id}/feedback")
async def feedback(session_id: str, body: FeedbackIn) -> dict[str, Any]:
    """Thumbs up/down and an optional line, from the "You're in" screen."""
    async with runtime.locks[session_id]:
        state = _load(session_id)
        if body.rating:
            state.feedback_rating = body.rating
        if body.text is not None:
            state.feedback_text = body.text.strip()[:1000] or None
        store.save(state)
    log.info("feedback %s: %s%s", session_id[:8], state.feedback_rating, " + comment" if state.feedback_text else "")
    return {"state": state.public_view()}


@app.get("/api/conversations")
def conversations(token: str = "", limit: int = 10) -> dict[str, Any]:
    """The owner's view of recent conversations, for reading how testers got on.

    Locked with METRICS_TOKEN, and off entirely when none is set. Transcripts and what was
    collected only: no Google tokens, no calendar or inbox data.
    """
    if not settings.metrics_token or token != settings.metrics_token:
        raise HTTPException(401, "token required")
    states = sorted(store.session_states(0), key=lambda s: s.updated_at, reverse=True)
    out = []
    for s in [s for s in states if s.user_turns > 0][: max(1, min(limit, 50))]:
        out.append({
            "session": s.session_id[:8],
            "updated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(s.updated_at)),
            "collected": {k: getattr(s, k) for k in ("agent_name", "user_name", "help_topic", "gmail_status", "graduated")},
            "call_status": s.call_status,
            "feedback": {"rating": s.feedback_rating, "text": s.feedback_text},
            "transcript": [{"role": t.role, "channel": t.channel, "text": t.text} for t in s.transcript],
        })
    return {"conversations": out}


@app.get("/api/metrics")
def metrics(days: float = 30, token: str = "") -> dict[str, Any]:
    from .metrics import compute

    if settings.metrics_token and token != settings.metrics_token:
        raise HTTPException(401, "metrics token required")
    since = time.time() - max(0.01, min(days, 365)) * 86400
    return compute(store.turn_rows(since), store.session_states(since))


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
    if body.type == "draft_confirmed":
        body.data["result"] = await _save_pending_draft(session_id, str(body.data.get("body") or ""))
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


class PhoneIn(BaseModel):
    phone: str = Field(min_length=7, max_length=24)


@app.post("/api/sessions/{session_id}/phone-call")
async def phone_call(session_id: str, body: PhoneIn) -> dict[str, Any]:
    """#13: Persona calls the user's real phone. Same pipeline and brain as the browser call."""
    from .voice import phone

    problem = settings.phone_problem()
    if problem:
        raise HTTPException(503, f"phone calls unavailable: {problem}")
    number = phone.normalize_number(body.phone)
    if not number:
        raise HTTPException(422, "That doesn't look like a phone number.")
    async with runtime.locks[session_id]:
        state = _load(session_id)
        state.phone = number
        apply_event(state, "call_accepted", {})  # ringing
        store.save(state)
    try:
        await phone.place_call(settings, number, session_id)
    except Exception:  # noqa: BLE001
        log.exception("Twilio call failed for session %s", session_id[:8])
        raise HTTPException(502, "Couldn't place the call. Check the number and try again.")
    return {"state": _load(session_id).public_view()}


@app.websocket("/api/twilio/stream")
async def twilio_stream(ws: WebSocket) -> None:
    """Twilio Media Streams: read the 'start' message for the session, then run the call."""
    from .voice.call import VoiceCall, end_call

    await ws.accept()
    start: dict[str, Any] = {}
    for _ in range(5):  # 'connected', then 'start'
        msg = json.loads(await ws.receive_text())
        if msg.get("event") == "start":
            start = msg.get("start") or {}
            break
    session_id = str((start.get("customParameters") or {}).get("session_id") or "")
    state = store.get(session_id) if session_id else None
    if not start.get("streamSid") or state is None or state.call_status not in ("ringing", "in_progress"):
        await ws.close()
        return
    await end_call(session_id)
    call = VoiceCall(session_id, phone_ws=ws, twilio={"stream_sid": start["streamSid"], "call_sid": start.get("callSid")})
    await call.run()


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
        from . import orchestrator as orch

        orch.refresh_insights(st)
        store.save(st)
    log.info(
        "Google connected for session %s: %d events, %d subjects",
        session_id[:8], len(snapshot["events"]), len(snapshot["emails"]),
    )
    return _popup_result({"ok": True})
