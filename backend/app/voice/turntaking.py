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

Threshold note for the README: the design suggests "shorter than ~400ms". Voice
activity alone can't tell "mhm" from "wait", so we decide on the words instead:
at most MAX_BACKCHANNEL_WORDS words, all from the backchannel list. Interim
transcripts arrive in ~200-300ms, so a real interruption still cuts in fast.
"""

from __future__ import annotations

import re
from typing import Literal

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.base_user_turn_start_strategy import BaseUserTurnStartStrategy

MAX_BACKCHANNEL_WORDS = 3

BACKCHANNELS = {
    "mhm", "mm", "mmhm", "mmhmm", "hmm", "hm", "uh-huh", "uhhuh", "uh", "huh", "yeah", "yep", "yup", "yes",
    "right", "okay", "ok", "k", "sure", "got", "it", "cool", "nice", "totally", "true", "gotcha", "ah", "oh",
    "wow", "alright", "i", "see", "exactly", "fair", "makes", "sense",
}
# Always a real interruption, even if short.
STOP_PHRASES = ("wait", "no", "stop", "actually", "hold on", "hang on", "sorry", "excuse me", "hey", "um no")

_WORD_RE = re.compile(r"[a-z]+(?:[-'][a-z]+)*")

BargeIn = Literal["backchannel", "interrupt", "undecided"]


def classify_barge_in(text: str) -> BargeIn:
    """Classify speech heard while the agent is talking."""
    t = text.lower().strip()
    words = _WORD_RE.findall(t)
    if not words:
        return "undecided"
    padded = f" {' '.join(words)} "
    if any(f" {p} " in padded for p in STOP_PHRASES):
        return "interrupt"
    if len(words) <= MAX_BACKCHANNEL_WORDS and all(w in BACKCHANNELS for w in words):
        return "backchannel"
    if len(words) >= 2:
        return "interrupt"
    return "undecided"  # one unknown word so far: wait for the interim transcript to grow


class BackchannelAwareStartStrategy(BaseUserTurnStartStrategy):
    """Starts a user turn, ignoring listening noises while the agent is talking.

    Agent silent: voice activity starts the turn right away (fast, like Pipecat's
    default). Agent speaking: wait for the words and apply classify_barge_in.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._bot_speaking = False
        self.backchannels_ignored = 0

    async def handle_user_turn_started(self):
        # Once the user has the floor, the agent has stopped.
        self._bot_speaking = False

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, VADUserStartedSpeakingFrame) and not self._bot_speaking:
            await self.trigger_user_turn_started()
            return ProcessFrameResult.STOP
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            if not self._bot_speaking:
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            verdict = classify_barge_in(frame.text)
            if verdict == "interrupt":
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            if verdict == "backchannel" and isinstance(frame, TranscriptionFrame):
                # Final "mhm": drop it so it isn't glued onto the next real turn.
                self.backchannels_ignored += 1
                await self.trigger_reset_aggregation()
        return ProcessFrameResult.CONTINUE
