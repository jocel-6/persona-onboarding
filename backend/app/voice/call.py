"""The phone call: a cascaded voice pipeline around the same brain the chat uses.

    browser mic --WebRTC--> Deepgram STT -> ConfidenceTagger
        -> user aggregator (Silero VAD, Smart Turn v3, backchannel filter, silence timer)
        -> BrainService (orchestrator + Claude, exactly as in text)
        -> TTS (sentence by sentence) --WebRTC--> browser speaker
        -> assistant aggregator (reports what the user actually heard)

Pipecat owns audio and turn-taking. Our brain owns the conversation, so a hangup
continues over text with the same state and history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    UserIdleTimeoutUpdateFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from .. import runtime
from ..events import apply_event
from ..state import OnboardingState
from .tts import make_tts
from .turntaking import BackchannelAwareStartStrategy

log = logging.getLogger("persona.voice")

LOW_CONFIDENCE = 0.6
LATENCY_LOG = Path(runtime.settings.db_path).parent / "latency.jsonl"

# session_id -> the live call, so HTTP events (Gmail connected, "I'm ready") are spoken.
active_calls: dict[str, VoiceCall] = {}


def _last_user_text(context: LLMContext) -> str:
    for msg in reversed(context.get_messages()):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return " ".join(p.get("text", "") for p in content if isinstance(p, dict)).strip()
    return ""


class ConfidenceTagger(FrameProcessor):
    """Notes words Deepgram wasn't sure about, so the brain can confirm names lightly."""

    def __init__(self, call: VoiceCall):
        super().__init__()
        self.call = call

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            try:
                words = frame.result.channel.alternatives[0].words  # Deepgram live result
                for w in words:
                    conf = getattr(w, "confidence", 1.0)
                    word = getattr(w, "punctuated_word", None) or getattr(w, "word", "")
                    if conf < LOW_CONFIDENCE and len(word) > 2:
                        self.call.brain_svc.low_confidence.append(word.strip(".,!?"))
            except (AttributeError, IndexError, TypeError):
                pass
        await self.push_frame(frame, direction)


class BrainService(FrameProcessor):
    """Runs our brain on each finished user turn and streams its words to TTS."""

    def __init__(self, call: VoiceCall):
        super().__init__()
        self.call = call
        self._turn: asyncio.Task | None = None
        self.low_confidence: list[str] = []
        self._was_interrupted = False
        self._silences = 0
        self._t_turn_end: float | None = None
        self._t_first_token: float | None = None
        self._ttft_ms: int | None = None

    # ---- frames ---------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            text = _last_user_text(frame.context)
            if text:
                await self.start_turn(user_text=text)
            return  # consumed: we are the "LLM"

        if isinstance(frame, InterruptionFrame):
            if self._turn and not self._turn.done():
                self._was_interrupted = True
            await self._cancel_turn()
        elif isinstance(frame, BotStartedSpeakingFrame) and self._t_turn_end is not None:
            self._log_latency()

        await self.push_frame(frame, direction)

    # ---- turns ----------------------------------------------------------

    async def start_turn(self, *, user_text: str | None = None, event_text: str | None = None):
        await self._cancel_turn()
        self._turn = self.create_task(self._run_turn(user_text, event_text), name="brain-turn")

    async def _cancel_turn(self):
        if self._turn and not self._turn.done():
            await self.cancel_task(self._turn)
        self._turn = None

    async def _run_turn(self, user_text: str | None, event_text: str | None):
        annotations: list[str] = []
        if user_text is not None:
            if self._was_interrupted:
                annotations.append("[you were interrupted: they only heard the start of your last reply]")
            if self.low_confidence:
                annotations.append(f"[low transcription confidence: {', '.join(dict.fromkeys(self.low_confidence))}]")
            self._was_interrupted = False
            self.low_confidence = []
            self._silences = 0
            await self._set_idle_timeout(runtime.settings.silence_checkin_secs)

        self._t_turn_end = time.perf_counter()
        self._t_first_token = None
        sid = self.call.session_id
        async with runtime.locks[sid]:
            state = runtime.store.get(sid)
            if state is None or state.call_status != "in_progress":
                return
            gen = runtime.brain.run_turn(state, user_text=user_text, event_text=event_text, annotations=annotations)
            started = finished = False
            try:
                async for ev in gen:
                    if ev["type"] == "delta":
                        if not started:
                            started = True
                            self._t_first_token = time.perf_counter()
                            await self.push_frame(LLMFullResponseStartFrame())
                        await self.push_frame(LLMTextFrame(ev["text"]))
                        await self._send(ev)
                    elif ev["type"] == "ui":
                        await self._send(ev)
                    elif ev["type"] == "done":
                        self._ttft_ms = ev["latency"]["ttft_ms"]
                        await self._send(ev)
                finished = True
            finally:
                await gen.aclose()  # repairs history if we were cut off
                runtime.store.save(state)
                if finished:
                    if started:
                        await self.push_frame(LLMFullResponseEndFrame())
                    await self._send({"type": "state", "state": state.public_view()})

    async def trim_to_heard(self, heard: str):
        """After a barge-in, keep only what the user actually heard in the history."""
        sid = self.call.session_id
        async with runtime.locks[sid]:
            state = runtime.store.get(sid)
            if state is None:
                return
            _trim_last_agent_turn(state, heard.strip())
            runtime.store.save(state)
            await self._send({"type": "state", "state": state.public_view()})

    # ---- silence --------------------------------------------------------

    async def on_idle(self):
        self._silences += 1
        s = runtime.settings
        if self._silences == 1:
            await self._set_idle_timeout(s.silence_offer_text_secs)
            await self.start_turn(
                event_text=(
                    f"{s.silence_checkin_secs:.0f}s of silence on the call. Check in once, gently and briefly "
                    "(no pressure, take your time). Don't repeat anything you've already said."
                )
            )
        elif self._silences == 2:
            await self.start_turn(
                event_text=(
                    f"{s.silence_offer_text_secs:.0f}s more silence. Lightly offer to switch to texting instead "
                    "(the chat is right there on their screen). Don't repeat yourself."
                )
            )
        # After that, stay quiet: nagging is worse than silence.

    async def on_user_started(self):
        if self._silences:
            self._silences = 0
            await self._set_idle_timeout(runtime.settings.silence_checkin_secs)

    async def _set_idle_timeout(self, secs: float):
        await self.push_frame(UserIdleTimeoutUpdateFrame(timeout=secs), FrameDirection.UPSTREAM)

    # ---- helpers --------------------------------------------------------

    async def _send(self, data: dict[str, Any]):
        await self.push_frame(RTVIServerMessageFrame(data=data))

    def _log_latency(self):
        now = time.perf_counter()
        entry = {
            "ts": time.time(),
            "session": self.call.session_id[:8],
            "llm_first_token_ms": self._ttft_ms,
            "turn_end_to_first_token_ms": round((self._t_first_token - self._t_turn_end) * 1000)
            if self._t_first_token
            else None,
            "turn_end_to_first_audio_ms": round((now - self._t_turn_end) * 1000),
        }
        self._t_turn_end = None
        log.info("voice latency %s", entry)
        try:
            LATENCY_LOG.parent.mkdir(parents=True, exist_ok=True)
            with LATENCY_LOG.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass


def _trim_last_agent_turn(state: OnboardingState, heard: str) -> None:
    marker = " [cut off here: the user interrupted]"
    for msg in reversed(state.messages):
        if msg["role"] != "assistant":
            continue
        others = [b for b in msg["content"] if b["type"] != "text"]
        msg["content"] = [{"type": "text", "text": (heard or "(nothing)") + marker}] + [
            b for b in others if b["type"] != "thinking"
        ]
        break
    for turn in reversed(state.transcript):
        if turn.role == "agent":
            turn.text = (heard + "…") if heard else "…"
            break


class VoiceCall:
    def __init__(self, session_id: str, connection: SmallWebRTCConnection):
        self.session_id = session_id
        self.connection = connection
        self.brain_svc = BrainService(self)
        self.task: PipelineTask | None = None

    def _keyterms(self) -> list[str]:
        state = runtime.store.get(self.session_id)
        names = [state.agent_name, state.user_name, state.google_name] if state else []
        return [n for n in names if n]

    def build(self) -> PipelineTask:
        s = runtime.settings
        transport = SmallWebRTCTransport(
            webrtc_connection=self.connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        stt_settings: dict[str, Any] = {"model": s.stt_model, "interim_results": True, "punctuate": True}
        if keyterms := self._keyterms():
            stt_settings["keyterm"] = keyterms  # names transcribed right, every time
        stt = DeepgramSTTService(api_key=s.deepgram_api_key, settings=DeepgramSTTService.Settings(**stt_settings))
        context = LLMContext()
        aggregators = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(),
                user_turn_strategies=UserTurnStrategies(start=[BackchannelAwareStartStrategy()]),
                user_idle_timeout=s.silence_checkin_secs,
            ),
        )
        user_agg, assistant_agg = aggregators.user(), aggregators.assistant()

        @user_agg.event_handler("on_user_turn_idle")
        async def _idle(_agg):
            await self.brain_svc.on_idle()

        @user_agg.event_handler("on_user_turn_started")
        async def _started(_agg, *_args):
            await self.brain_svc.on_user_started()

        @assistant_agg.event_handler("on_assistant_turn_stopped")
        async def _assistant_stopped(_agg, message):
            if getattr(message, "interrupted", False):
                await self.brain_svc.trim_to_heard(message.content or "")

        @transport.event_handler("on_client_connected")
        async def _connected(_transport, _client):
            await self._on_connected()

        @transport.event_handler("on_client_disconnected")
        async def _disconnected(_transport, _client):
            await self.stop()

        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                ConfidenceTagger(self),
                user_agg,
                self.brain_svc,
                make_tts(s),
                transport.output(),
                assistant_agg,
            ]
        )
        self.task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True))
        return self.task

    async def _on_connected(self):
        async with runtime.locks[self.session_id]:
            state = runtime.store.get(self.session_id)
            if state is None:
                return
            event_text, ui = apply_event(state, "call_connected", {})
            runtime.store.save(state)
        for ev in ui:
            await self.brain_svc._send({"type": "ui", "ui": ev})
        if event_text:
            await self.brain_svc.start_turn(event_text=event_text)

    async def say_event(self, event_text: str):
        """Run an event turn on the call (e.g. Gmail connected mid-call): spoken, not texted."""
        await self.brain_svc.start_turn(event_text=event_text)

    async def run(self):
        active_calls[self.session_id] = self
        try:
            await PipelineRunner(handle_sigint=False).run(self.build())
        except Exception:
            log.exception("voice call %s crashed", self.session_id[:8])
        finally:
            if active_calls.get(self.session_id) is self:
                del active_calls[self.session_id]

    async def stop(self):
        if self.task:
            await self.task.cancel()


async def end_call(session_id: str) -> None:
    call = active_calls.pop(session_id, None)
    if call:
        await call.stop()
