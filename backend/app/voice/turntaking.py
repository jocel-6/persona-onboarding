"""Turn-taking: backchannel filtering on top of Pipecat's user-turn strategies.

People say "mhm", "yeah", "right" while someone else talks, just to show they're
listening. A naive agent treats that as an interruption and stops mid-sentence.
Tech design 4.2:

    while the agent is speaking and the user makes a sound:
        short backchannel ("mhm", "yeah", ...)  -> keep talking
        contains a stop word ("wait", "no", ...) -> stop immediately and listen
        anything else                            -> stop, and listen

Only filtered while the agent is speaking: after it asks a question, "yeah" is an
answer, so it starts a turn as usual. End-of-turn detection (is the user done?)
is Pipecat's Smart Turn v3 model, which listens to the audio, so "I'm, uh..."
waits and "I'm Sam." responds fast.

Words, not sound, take the floor. Background noise (a TV, a fan, a kitchen) trips
voice-activity detection all the time; if raw sound could start a turn, the agent got
cut off mid-sentence, or a reply it was still writing got thrown away, before the user
had said a thing. So a turn starts only once the transcriber hears real words:
  * a stop word ("wait", "actually", ...) interrupts as soon as the interim shows it;
  * two or more non-backchannel words interrupt;
  * one unknown word interrupts if speech is still going BARGE_IN_SECS (0.6s) after it
    started ("Maya..." keeps going; a cough transcribed as "a" doesn't). A spoken
    "mhm" ran close to 400ms in testing, so 0.6s leaves margin;
  * all-backchannel transcripts ("mhm", "yeah") never interrupt while it talks.
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    ProposedUserStartedSpeakingFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.base_user_turn_start_strategy import BaseUserTurnStartStrategy

MAX_BACKCHANNEL_WORDS = 3
# Speech that keeps going this long while the agent talks is a real interruption,
# even before any words are transcribed. "Mhm" and "yeah" are shorter than this.
BARGE_IN_SECS = 0.6

BACKCHANNELS = {
    "mhm", "mm", "mmhm", "mmhmm", "hmm", "hm", "uh-huh", "uhhuh", "uh", "huh", "yeah", "yep", "yup", "yes",
    "right", "okay", "ok", "k", "sure", "got", "it", "cool", "nice", "totally", "true", "gotcha", "ah", "oh",
    "wow", "alright", "i", "see", "exactly", "fair", "makes", "sense",
}
# Always a real interruption, even if short.
STOP_PHRASES = ("wait", "no", "stop", "actually", "hold on", "hang on", "sorry", "excuse me", "hey", "um no")

_WORD_RE = re.compile(r"[a-z]+(?:[-'][a-z]+)*")
# Listening noises however the transcriber spells them: mm, mhm, mhmm, mmhmm, hmm, uh-huh, uh huh.
_NOISE_RE = re.compile(r"^(m+|m*h*m+h*m*|h+m+|u+h+|u+h+-?h+u+h+|a+h+|o+h+)$")


def _is_backchannel_word(w: str) -> bool:
    return w in BACKCHANNELS or bool(_NOISE_RE.match(w))

BargeIn = Literal["backchannel", "interrupt", "undecided"]


def classify_barge_in(text: str) -> BargeIn:
    """Classify speech heard while the agent is talking."""
    t = text.lower().strip()
    words = _WORD_RE.findall(t)
    if not words:
        return "undecided"
    padded = f" {' '.join(words)} "
    if any(f" {p} " in padded for p in STOP_PHRASES) or any(w.startswith("wait") for w in words):
        return "interrupt"
    if len(words) <= MAX_BACKCHANNEL_WORDS and all(_is_backchannel_word(w) for w in words):
        return "backchannel"
    if len(words) >= 2:
        return "interrupt"
    return "undecided"  # one unknown word so far: wait for the interim transcript to grow


_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]*")


def strip_leading_backchannels(text: str) -> str:
    """Drop listening noises heard while the agent talked from the start of a user turn.

    "Mhmm. Sorry, I just meant..." -> "Sorry, I just meant...". A turn that is only
    a backchannel is kept: then it's an answer ("Yeah.").
    """
    sentences = [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]
    while len(sentences) > 1 and classify_barge_in(sentences[0]) == "backchannel":
        sentences.pop(0)
    return " ".join(sentences) if sentences else text


class BackchannelAwareStartStrategy(BaseUserTurnStartStrategy):
    """Starts a user turn, ignoring listening noises while the agent is talking.

    Agent silent: the first transcribed words start the turn, or Deepgram Flux's own
    start-of-turn (its speech model, which ignores plain noise far better than VAD; Flux
    only sends words at the end of a turn, so without this the turn "started" only after
    they'd finished, and the silence check-in talked over them). Agent speaking: the words
    must be a real interruption (classify_barge_in). Raw voice activity never starts a
    turn; it only times how long an ambiguous single word keeps going.
    """

    @property
    def resolves_proposed_turn_start_frames(self) -> bool:
        return True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._bot_speaking = False
        self._heard_word = False  # an "undecided" word: real speech, not yet a clear interruption
        self._barge_timer: asyncio.Task | None = None
        self.backchannels_ignored = 0

    async def handle_user_turn_started(self):
        # Once the user has the floor, the agent has stopped.
        self._bot_speaking = False
        await self._cancel_barge_timer()

    async def _cancel_barge_timer(self):
        if self._barge_timer and not self._barge_timer.done():
            self._barge_timer.cancel()
        self._barge_timer = None

    async def _barge_in_after_delay(self):
        await asyncio.sleep(BARGE_IN_SECS)
        if self._bot_speaking and self._heard_word:
            await self.trigger_user_turn_started()

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            await self._cancel_barge_timer()
        elif isinstance(frame, ProposedUserStartedSpeakingFrame):
            if not self._bot_speaking:
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            # While it talks, Flux's start is only a hint: the words decide (below).
            self._heard_word = False
            await self._cancel_barge_timer()
            self._barge_timer = asyncio.create_task(self._barge_in_after_delay())
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            # Sound alone is not a turn (it may be the TV). While the agent talks, time it:
            # if it turns out to be a word that keeps going, that's an interruption.
            if self._bot_speaking:
                self._heard_word = False
                await self._cancel_barge_timer()
                self._barge_timer = asyncio.create_task(self._barge_in_after_delay())
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._cancel_barge_timer()
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            if not self._bot_speaking:
                if _WORD_RE.search(frame.text.lower()):
                    await self.trigger_user_turn_started()
                    return ProcessFrameResult.STOP
                return ProcessFrameResult.CONTINUE
            verdict = classify_barge_in(frame.text)
            if verdict == "interrupt":
                await self._cancel_barge_timer()
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            self._heard_word = verdict == "undecided" and bool(_WORD_RE.search(frame.text.lower()))
            if verdict == "backchannel" and isinstance(frame, TranscriptionFrame):
                # Final "mhm": drop it so it isn't glued onto the next real turn.
                self.backchannels_ignored += 1
                await self.trigger_reset_aggregation()
        return ProcessFrameResult.CONTINUE


# Emoji, pictographs, and markdown syntax that TTS would read aloud or stumble over.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF\U00002B00-\U00002BFF\uFE0F\u200D]"
)
_MARKDOWN_RE = re.compile(r"[*_`#>|]+|\[(?=[^\]]*\]\()|\]\([^)]*\)")


def speakable(text: str) -> str:
    """Strip what shouldn't be spoken: emoji, markdown symbols, link targets.

    The prompt already asks for plain spoken replies on calls; this is the code-side
    guarantee, applied to each streamed chunk before it reaches text-to-speech.
    """
    return _MARKDOWN_RE.sub("", _EMOJI_RE.sub("", text))
