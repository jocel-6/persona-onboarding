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

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    InputAudioRawFrame,
    EagerEndOfTurnCancelFrame,
    TTSSpeakFrame,
    TTSUpdateSettingsFrame,
    UserStoppedSpeakingFrame,
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
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from .. import runtime
from ..events import apply_event
from ..state import OnboardingState
from .tts import make_tts
from .turntaking import BackchannelAwareStartStrategy, speakable, strip_leading_backchannels

log = logging.getLogger("persona.voice")

LOW_CONFIDENCE = 0.6

# Instant acknowledgements, played only while the real reply is still being written.
ACKS = ["Mm-hm.", "Got it.", "Okay.", "Mmm.", "Right."]
QUESTION_ACKS = ["Hmm.", "Ooh, good question.", "Hmm, let me think."]

# Tone-matched delivery (Cartesia generation_config). Documented emotions only.
TONES = {
    "frustrated": {"speed": 0.94, "emotion": "neutral"},  # slower, even, no pep
    "rushed": {"speed": 1.1, "emotion": "neutral"},
    "enthusiastic": {"speed": 1.03, "emotion": "excited"},
    "chatty": {"speed": 1.0, "emotion": "content"},
    "neutral": {"speed": 1.0, "emotion": "content"},
}


def pick_ack(user_text: str, sentiment: str, last: str | None) -> str | None:
    """A short spoken acknowledgement to cover the thinking gap. None when it'd feel wrong."""
    if sentiment == "frustrated":
        return None  # an "mm-hm" to someone annoyed reads as patronizing
    pool = QUESTION_ACKS if user_text.rstrip().endswith("?") else ACKS
    if sentiment == "rushed":
        pool = ["Okay.", "Got it."]
    choices = [a for a in pool if a != last] or pool
    return choices[int(time.time() * 1000) % len(choices)]
CLIENT_READY_FALLBACK_SECS = 2.0
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


class ToneTap(FrameProcessor):
    """#5: keeps the last few seconds of the user's audio so their tone can be analyzed."""

    KEEP_SECS = 8.0

    def __init__(self):
        super().__init__()
        self._chunks: list[bytes] = []
        self._bytes = 0
        self.sample_rate = 16000
        self.channels = 1

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.sample_rate, self.channels = frame.sample_rate, frame.num_channels
            self._chunks.append(frame.audio)
            self._bytes += len(frame.audio)
            limit = int(self.KEEP_SECS * self.sample_rate * self.channels * 2)
            while self._bytes > limit and self._chunks:
                self._bytes -= len(self._chunks.pop(0))
        await self.push_frame(frame, direction)

    def last(self, secs: float) -> bytes:
        pcm = b"".join(self._chunks)
        n = int(secs * self.sample_rate * self.channels * 2)
        return pcm[-n:] if n else pcm


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
        self._audio_started = False
        self._cancelled_by_user = False
        self._carryover: str | None = None  # a fragment waiting to be joined with the rest of the sentence
        # Speculative replies (Flux): output is held until the turn is confirmed.
        self._speculating = False
        self._speculated_text: str | None = None
        self._held: list[Frame] = []
        self._discard = False
        # Instant acknowledgements.
        self._ack_task: asyncio.Task | None = None
        self._reply_live = False
        self._last_ack: str | None = None
        self._turn_sentiment = "neutral"
        self._tone: str | None = None
        self._first_audio_via: str | None = None
        self._voice_tone: str | None = None  # #5: what their voice sounded like last turn
        # Which assistant message / transcript line the last voice turn produced,
        # so a barge-in trims exactly that one (never a later text reply).
        self._last_turn_ref: tuple[int, int] | None = None

    # ---- frames ---------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            text = strip_leading_backchannels(_last_user_text(frame.context))
            if text:
                await self.start_turn(user_text=text, speculative=bool(getattr(frame, "speculation", False)))
            return  # consumed: we are the "LLM"

        if isinstance(frame, EagerEndOfTurnCancelFrame) and self._speculating:
            # They weren't done after all: throw the speculative reply away, as if it never ran.
            self._discard = True
            await self._cancel_turn()
            self._discard = False
        elif isinstance(frame, UserStoppedSpeakingFrame) and self._speculating:
            await self._confirm_speculation()

        if isinstance(frame, InterruptionFrame):
            # Only a real interruption if they'd already started hearing this reply;
            # otherwise it was just a mid-thought pause and we simply wait for the rest.
            if self._turn and not self._turn.done() and self._audio_started:
                self._was_interrupted = True
            self._cancelled_by_user = True
            await self._cancel_turn()
            self._cancelled_by_user = False
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._audio_started = True
            if self._t_turn_end is not None:
                self._log_latency()

        await self.push_frame(frame, direction)

    # ---- turns ----------------------------------------------------------

    async def start_turn(self, *, user_text: str | None = None, event_text: str | None = None, speculative: bool = False):
        await self._cancel_turn()
        self._turn = self.create_task(self._run_turn(user_text, event_text, speculative), name="brain-turn")

    async def _cancel_turn(self):
        await self._cancel_ack()
        if self._turn and not self._turn.done():
            await self.cancel_task(self._turn)
        self._turn = None
        self._speculating = False
        self._held = []

    # ---- #5 tone of voice (in parallel; shapes the next turn) ----------------

    def _listen_to_tone(self, user_text: str) -> None:
        tap = self.call.tone_tap
        key = runtime.settings.hume_api_key
        if not tap or not key:
            return
        secs = min(ToneTap.KEEP_SECS, max(1.5, len(user_text.split()) / 2.5 + 0.8))  # ~2.5 words/sec
        pcm = tap.last(secs)
        if len(pcm) < tap.sample_rate:  # under half a second: nothing to hear
            return
        self.create_task(self._tone_task(key, pcm, tap.sample_rate, tap.channels), name="tone")

    async def _tone_task(self, key: str, pcm: bytes, rate: int, channels: int) -> None:
        from . import hume

        words, mood = hume.summarize(await hume.analyze(key, hume.to_wav(pcm, rate, channels)))
        self._voice_tone = words
        if mood:
            async with runtime.locks[self.call.session_id]:
                state = runtime.store.get(self.call.session_id)
                if state is not None and state.sentiment not in ("frustrated",):
                    state.sentiment = mood
                    runtime.store.save(state)

    # ---- output: live, or held while speculating --------------------------

    async def _out(self, frame: Frame) -> None:
        if self._speculating:
            self._held.append(frame)
            return
        if isinstance(frame, (LLMFullResponseStartFrame, LLMTextFrame)):
            if not self._reply_live:
                self._reply_live = True
                await self._cancel_ack()
                await self._apply_tone()
        await self.push_frame(frame)

    async def _confirm_speculation(self) -> None:
        """Their turn really ended: release the reply we already have (possibly all of it)."""
        self._speculating = False
        self._t_turn_end = time.perf_counter()
        held, self._held = self._held, []
        for f in held:
            await self._out(f)
        if not self._reply_live:
            self._schedule_ack(self._speculated_text or "")

    # ---- instant acknowledgements -------------------------------------------

    def _schedule_ack(self, user_text: str) -> None:
        if not runtime.settings.voice_acks or self._reply_live:
            return
        self._ack_task = self.create_task(self._ack_after_delay(user_text), name="ack")

    async def _ack_after_delay(self, user_text: str) -> None:
        await asyncio.sleep(runtime.settings.ack_delay_secs)
        if self._reply_live or self._speculating or self._audio_started:
            return
        ack = pick_ack(user_text, self._turn_sentiment, self._last_ack)
        if ack:
            self._last_ack = ack
            self._first_audio_via = "ack"
            await self.push_frame(TTSSpeakFrame(ack, append_to_context=False))

    async def _cancel_ack(self) -> None:
        if self._ack_task and not self._ack_task.done():
            await self.cancel_task(self._ack_task)
        self._ack_task = None

    # ---- tone-matched delivery ------------------------------------------------

    async def _apply_tone(self) -> None:
        s = runtime.settings
        if not s.voice_tone or s.tts_provider != "cartesia":
            return
        tone = self._turn_sentiment if self._turn_sentiment in TONES else "neutral"
        if tone == self._tone:
            return
        self._tone = tone
        from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig

        await self.push_frame(
            TTSUpdateSettingsFrame(delta=CartesiaTTSService.Settings(generation_config=GenerationConfig(**TONES[tone])))
        )

    async def _run_turn(self, user_text: str | None, event_text: str | None, speculative: bool = False):
        annotations: list[str] = []
        self._speculating = speculative
        self._speculated_text = user_text if speculative else None
        self._held = []
        self._reply_live = False
        self._first_audio_via = None
        if user_text is not None and self._carryover:
            # The start of this thought was cut off by a pause; hear it as one sentence.
            user_text = f"{self._carryover} {user_text}"
            self._carryover = None
        if user_text is not None:
            if self._was_interrupted:
                annotations.append("[you were interrupted: they only heard the start of your last reply]")
            if self.low_confidence:
                annotations.append(f"[low transcription confidence: {', '.join(dict.fromkeys(self.low_confidence))}]")
            if self._voice_tone:
                annotations.append(f"[their voice sounded: {self._voice_tone}]")
                self._voice_tone = None
            self._listen_to_tone(user_text)
            self._was_interrupted = False
            self.low_confidence = []
            self._silences = 0
            await self._set_idle_timeout(runtime.settings.silence_checkin_secs)

        self._t_turn_end = None if speculative else time.perf_counter()
        self._t_first_token = None
        self._audio_started = False
        sid = self.call.session_id
        async with runtime.locks[sid]:
            state = runtime.store.get(sid)
            if state is None or state.call_status != "in_progress":
                return
            self._turn_sentiment = state.sentiment
            if user_text is not None and not speculative:
                self._schedule_ack(user_text)
            before = state.model_copy(deep=True)  # to undo a turn that was only a fragment
            gen = runtime.brain.run_turn(state, user_text=user_text, event_text=event_text, annotations=annotations)
            started = finished = False
            try:
                async for ev in gen:
                    if ev["type"] == "delta":
                        if not started:
                            started = True
                            self._t_first_token = time.perf_counter()
                            await self._out(LLMFullResponseStartFrame())
                        if spoken := speakable(ev["text"]):
                            await self._out(LLMTextFrame(spoken))
                        await self._send(ev)
                    elif ev["type"] == "ui":
                        await self._send(ev)
                    elif ev["type"] == "done":
                        from ..main import record_turn_metrics

                        record_turn_metrics(sid, "voice", ev)
                        await self._send(ev)
                finished = True
            finally:
                await gen.aclose()  # repairs history if we were cut off
                if not finished and (self._discard or speculative):
                    # A withdrawn speculation: the user never heard it, so it never happened. No
                    # carryover either: Flux's committed transcript already has the whole turn.
                    state = before
                    self._last_turn_ref = None
                elif not finished and user_text is not None and self._cancelled_by_user and not self._audio_started:
                    # They kept talking before hearing a word: that was a fragment of one thought
                    # ("What" ... "did I mention?"). Undo this turn and merge it into the next.
                    state = before
                    self._carryover = user_text
                    self._last_turn_ref = None
                else:
                    self._last_turn_ref = (len(state.messages) - 1, len(state.transcript) - 1)
                runtime.store.save(state)
                if finished:
                    if started:
                        await self._out(LLMFullResponseEndFrame())
                    await self._send({"type": "state", "state": state.public_view()})

    async def trim_to_heard(self, heard: str):
        """After a barge-in, keep only what the user actually heard in the history."""
        ref, self._last_turn_ref = self._last_turn_ref, None
        if ref is None or self.call.stopping:
            return  # a hangup isn't a barge-in; the call is over
        sid = self.call.session_id
        async with runtime.locks[sid]:
            state = runtime.store.get(sid)
            if state is None:
                return
            _trim_agent_turn(state, ref, heard.strip())
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
        await self._out(RTVIServerMessageFrame(data=data))

    def _log_latency(self):
        now = time.perf_counter()
        entry = {
            "ts": time.time(),
            "session": self.call.session_id[:8],
            "turn_end_to_first_token_ms": round((self._t_first_token - self._t_turn_end) * 1000)
            if self._t_first_token
            else None,
            "turn_end_to_first_audio_ms": round((now - self._t_turn_end) * 1000),
            "first_audio": self._first_audio_via or "reply",
            "stt": runtime.settings.stt_engine,
        }
        self._t_turn_end = None
        runtime.store.record_first_audio(self.call.session_id, entry["turn_end_to_first_audio_ms"], entry["first_audio"])
        log.info("voice latency %s", entry)
        try:
            LATENCY_LOG.parent.mkdir(parents=True, exist_ok=True)
            with LATENCY_LOG.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass


def _trim_agent_turn(state: OnboardingState, ref: tuple[int, int], heard: str) -> None:
    """Cut the voice turn at `ref` (message index, transcript index) to what was heard."""
    msg_i, turn_i = ref
    marker = " [cut off here: the user interrupted]"
    if 0 <= msg_i < len(state.messages) and state.messages[msg_i]["role"] == "assistant":
        msg = state.messages[msg_i]
        others = [b for b in msg["content"] if b["type"] not in ("text", "thinking")]
        msg["content"] = [{"type": "text", "text": (heard or "(nothing)") + marker}] + others
    if 0 <= turn_i < len(state.transcript) and state.transcript[turn_i].role == "agent":
        state.transcript[turn_i].text = (heard + "…") if heard else "…"


class VoiceCall:
    def __init__(self, session_id: str, connection: SmallWebRTCConnection):
        self.session_id = session_id
        self.connection = connection
        self.brain_svc = BrainService(self)
        self.task: PipelineTask | None = None
        self._greeted = False
        self.stopping = False
        self.tone_tap: ToneTap | None = None
        self._fallback: asyncio.TimerHandle | None = None
        self._trim_task: asyncio.Task | None = None

    def _voice_id(self) -> str | None:
        state = runtime.store.get(self.session_id)
        chosen = state.voice_id if state else None
        return chosen if chosen in {v["id"] for v in runtime.settings.voice_choices()} else None

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
        keyterms = [*self._keyterms(), "Persona", "Gmail"]  # names right every time, plus onboarding words
        if s.stt_engine == "flux":
            from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
            from pipecat.turns.user_stop import EagerUserTurnStopStrategy

            # Flux decides end of turn itself and predicts it early ("eager"), which is what lets
            # the reply be written during the user's final pause. Interruptions stay with our
            # backchannel-aware start strategy, so Flux doesn't interrupt on its own.
            stt = DeepgramFluxSTTService(
                api_key=s.deepgram_api_key,
                should_interrupt=False,
                enable_eager_end_of_turn=True,
                settings=DeepgramFluxSTTService.Settings(keyterm=keyterms, eager_eot_threshold=0.5, eot_threshold=0.75),
            )
            stop_strategies = [EagerUserTurnStopStrategy()]
        else:
            stt = DeepgramSTTService(
                api_key=s.deepgram_api_key,
                settings=DeepgramSTTService.Settings(
                    model=s.stt_model,
                    language="en",
                    interim_results=True,
                    punctuate=True,
                    smart_format=True,  # "two PM" -> "2 PM", emails and numbers formatted
                    keyterm=keyterms,
                ),
            )
            stop_strategies = [
                TurnAnalyzerUserTurnStopStrategy(
                    turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=s.turn_max_wait_secs))
                )
            ]
        context = LLMContext()
        aggregators = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(),
                user_turn_strategies=UserTurnStrategies(start=[BackchannelAwareStartStrategy()], stop=stop_strategies),
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
                # Never wait on the session lock inside a pipeline handler: the next brain
                # turn holds that lock while it pushes frames through this pipeline, so
                # waiting here deadlocks the call (it went silent after an interruption).
                self._trim_task = asyncio.create_task(self.brain_svc.trim_to_heard(message.content or ""))

        @transport.event_handler("on_client_connected")
        async def _connected(_transport, _client):
            # Prefer starting on the browser's client-ready (data channel and speaker are
            # set up by then); fall back after a moment in case it never arrives.
            self._fallback = asyncio.get_running_loop().call_later(
                CLIENT_READY_FALLBACK_SECS, lambda: asyncio.ensure_future(self._on_connected())
            )

        @transport.event_handler("on_client_disconnected")
        async def _disconnected(_transport, _client):
            await self.stop()

        self.tone_tap = ToneTap() if s.hume_api_key else None
        pipeline = Pipeline(
            [
                transport.input(),
                *([self.tone_tap] if self.tone_tap else []),
                stt,
                ConfidenceTagger(self),
                user_agg,
                self.brain_svc,
                make_tts(s, voice_id=self._voice_id()),
                transport.output(),
                assistant_agg,
            ]
        )
        self.task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True))

        @self.task.rtvi.event_handler("on_client_ready")
        async def _client_ready(_rtvi):
            await self._on_connected()

        return self.task

    async def _on_connected(self):
        if self._greeted or self.stopping:
            return  # a stale timer must never revive a call that already ended
        self._greeted = True
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
        self.stopping = True
        if self._fallback:
            self._fallback.cancel()
        if self.task:
            await self.task.cancel()


async def end_call(session_id: str) -> None:
    call = active_calls.pop(session_id, None)
    if call:
        await call.stop()
