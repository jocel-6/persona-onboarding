"""Voice-layer logic that doesn't need audio: barge-in rules and history repair."""

import asyncio
from types import SimpleNamespace as NS

from app.brain import Brain
from app.config import Settings
from app.state import OnboardingState, Turn
from app.voice.call import _trim_agent_turn
from app.voice.turntaking import classify_barge_in, strip_leading_backchannels
from tests.test_brain import FakeClient


def test_backchannels_are_ignored_but_real_interruptions_stop_the_agent():
    for t in ["mhm", "Mhmm.", "mmhmm", "Mmm", "hmm", "uh huh", "yeah", "uh-huh right", "okay got it", "yeah yeah", "sure"]:
        assert classify_barge_in(t) == "backchannel", t
    for t in ["wait", "waits", "Waits. I", "no", "actually", "hold on", "sorry what", "yeah but that's not it", "I think so too"]:
        assert classify_barge_in(t) == "interrupt", t
    assert classify_barge_in("Maya") == "undecided"  # one unknown word: wait for more
    assert classify_barge_in("") == "undecided"


class SlowStream:
    """Streams a few words, then stalls like a long reply mid-sentence."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for w in ["So ", "tell ", "me "]:
                yield NS(type="text", text=w)
            await asyncio.sleep(30)
        return gen()


def test_cancel_mid_reply_leaves_valid_history():
    client = FakeClient([])
    client.stream = lambda **kw: SlowStream()
    brain = Brain(Settings(), client=client)
    s = OnboardingState(agent_name="Iris", call_status="in_progress", channel="voice")
    s.messages = [
        {"role": "user", "content": [{"type": "text", "text": "[event: call connected]"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Hey, it's Iris!"}]},
    ]

    async def go():
        gen = brain.run_turn(s, user_text="hi there")
        seen = []

        async def consume():
            async for ev in gen:
                seen.append(ev)

        t = asyncio.create_task(consume())
        while len(seen) < 3:
            await asyncio.sleep(0.01)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        await gen.aclose()

    asyncio.run(go())
    assert s.messages[-1]["role"] == "assistant"
    assert s.messages[-1]["content"][0]["text"] == "So tell me"
    roles = [m["role"] for m in s.messages]
    assert all(a != b for a, b in zip(roles, roles[1:]))
    assert s.transcript[-1].text == "So tell me…"


def test_trim_keeps_only_what_was_heard():
    s = OnboardingState()
    s.messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Nice! Once you connect Gmail I can sort all of that for you."},
                {"type": "tool_use", "id": "t1", "name": "save_onboarding_info", "input": {"user_name": "Mia"}},
            ],
        },
    ]
    s.transcript = [Turn(role="agent", text="Nice! Once you connect Gmail I can sort all of that.", channel="voice")]
    s.messages.append({"role": "user", "content": [{"type": "text", "text": "[event: hangup]"}]})
    s.messages.append({"role": "assistant", "content": [{"type": "text", "text": "Looks like we got cut off!"}]})
    _trim_agent_turn(s, (1, 0), "Nice! Once you connect")
    assert s.messages[-1]["content"][0]["text"] == "Looks like we got cut off!"  # later text reply untouched
    s.messages = s.messages[:2]
    content = s.messages[-1]["content"]
    assert content[0]["text"].startswith("Nice! Once you connect [cut off")
    assert content[1]["type"] == "tool_use"  # tool call still pairs with its pending result
    assert s.transcript[-1].text == "Nice! Once you connect…"


def test_leading_backchannels_are_dropped_from_a_turn():
    assert strip_leading_backchannels("Mhmm. Sorry. I just meant my calendar?") == "Sorry. I just meant my calendar?"
    assert strip_leading_backchannels("Yeah.") == "Yeah."  # on its own, it's an answer
    assert strip_leading_backchannels("Yeah, that works.") == "Yeah, that works."


def test_nothing_unspeakable_reaches_text_to_speech():
    from app.voice.turntaking import speakable

    assert speakable("Love it 🙌 **Juno** it is!") == "Love it  Juno it is!"
    assert speakable("See [the docs](https://x.y/z) — ok") == "See the docs — ok"
    assert speakable("You've got the dentist Thursday at 9:15.") == "You've got the dentist Thursday at 9:15."


def test_stale_sessions_are_revoked_then_deleted(monkeypatch):
    import os
    os.environ.setdefault("DB_PATH", ":memory:")
    from app import google, main, runtime

    revoked = []

    async def fake_revoke(tok):
        revoked.append(tok)

    monkeypatch.setattr(google, "revoke", fake_revoke)
    old = OnboardingState()
    runtime.store.save(old)
    runtime.store._conn.execute("UPDATE sessions SET updated_at = 0 WHERE id = ?", (old.session_id,))
    runtime.store.save_tokens(old.session_id, {"refresh_token": "rt-old"})
    fresh = OnboardingState()
    runtime.store.save(fresh)

    assert asyncio.run(main.purge_stale_sessions()) >= 1
    assert runtime.store.get(old.session_id) is None and runtime.store.get_tokens(old.session_id) is None
    assert "rt-old" in revoked and runtime.store.get(fresh.session_id) is not None


def test_acknowledgements_fit_the_moment():
    from app.voice.call import ACKS, QUESTION_ACKS, pick_ack

    assert pick_ack("I'm drowning in school emails", "neutral", None) in ACKS
    assert pick_ack("can you help with my calendar?", "neutral", None) in QUESTION_ACKS
    assert pick_ack("ugh whatever", "frustrated", None) is None  # "mm-hm" to someone annoyed feels patronizing
    assert pick_ack("fine", "rushed", None) in ("Okay.", "Got it.")
    for _ in range(20):
        assert pick_ack("fine", "rushed", "Okay.") == "Got it."  # never the same one twice in a row


def test_speculative_reply_is_held_then_released_on_confirm():
    from pipecat.frames.frames import LLMTextFrame

    from app.voice.call import BrainService

    pushed = []

    class FakeCall:
        session_id = "s"
        stopping = False

    bs = BrainService(FakeCall())

    async def fake_push(frame, direction=None):
        pushed.append(frame)

    bs.push_frame = fake_push

    async def go():
        bs._speculating = True
        bs._speculated_text = "what's on my calendar tomorrow?"
        await bs._out(LLMTextFrame("You've got"))
        await bs._out(LLMTextFrame(" three things."))
        assert not [f for f in pushed if isinstance(f, LLMTextFrame)]  # nothing reaches TTS yet
        await bs._confirm_speculation()
        texts = [f.text for f in pushed if isinstance(f, LLMTextFrame)]
        assert texts == ["You've got", " three things."]  # released in order, instantly
        assert bs._reply_live and bs._t_turn_end is not None

    asyncio.run(go())


def test_tone_of_voice_summaries_are_plain_and_only_confident():
    from app.voice.hume import summarize, to_wav

    words, mood = summarize({"Distress": 0.55, "Tiredness": 0.4, "Joy": 0.05, "Awkwardness": 0.9})
    assert words == "stressed, tired" and mood == "frustrated"  # unknown names ignored, weak ones dropped
    assert summarize({"Joy": 0.6, "Excitement": 0.5})[1] == "enthusiastic"
    assert summarize({"Calmness": 0.1}) == (None, None)
    assert to_wav(b"\x00\x00" * 16000, 16000, 1)[:4] == b"RIFF"


def test_a_confirmed_speculation_cut_off_by_more_speech_carries_over(monkeypatch):
    """Flux confirmed "I'm Jordan, and what I need is", then they kept going: keep the start."""
    import os
    os.environ.setdefault("DB_PATH", ":memory:")
    from app import runtime
    from app.voice.call import BrainService

    client = FakeClient([])
    client.stream = lambda **kw: SlowStream()
    monkeypatch.setattr(runtime, "brain", Brain(Settings(), client=client))
    s = OnboardingState(agent_name="Iris", call_status="in_progress", channel="voice")
    runtime.store.save(s)

    class FakeCall:
        session_id = s.session_id
        stopping = False

    bs = BrainService(FakeCall())

    async def nothing(*a, **k):
        pass

    bs.push_frame = bs._send = bs._set_idle_timeout = nothing
    bs._listen_to_tone = lambda *a: None
    bs.create_task = lambda coro, name=None: asyncio.get_running_loop().create_task(coro)

    async def cancel(task, timeout=None):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    bs.cancel_task = cancel

    async def go():
        await bs.start_turn(user_text="I'm Jordan, and what I need is", speculative=True)
        while not bs._held:
            await asyncio.sleep(0.01)
        await bs._confirm_speculation()
        bs._cancelled_by_user = True  # they kept talking before hearing a word
        await bs._cancel_turn()
        bs._cancelled_by_user = False

    asyncio.run(go())
    assert bs._carryover == "I'm Jordan, and what I need is"


def test_background_noise_never_takes_the_floor(monkeypatch):
    """Sound without words (a TV, a fan) must not cut the agent off or start a turn."""
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        InterimTranscriptionFrame,
        VADUserStartedSpeakingFrame,
    )

    from app.voice import turntaking as tt

    monkeypatch.setattr(tt, "BARGE_IN_SECS", 0.05)
    strat = tt.BackchannelAwareStartStrategy()
    started = []

    async def trigger():
        started.append(True)

    strat.trigger_user_turn_started = trigger

    def interim(text):
        return InterimTranscriptionFrame(text=text, user_id="u", timestamp="0")

    async def go():
        # Agent thinking (not speaking yet): noise alone must not throw its reply away.
        await strat.process_frame(VADUserStartedSpeakingFrame())
        await strat.process_frame(interim(""))
        assert not started
        # Agent speaking: long noise with no words keeps it talking.
        await strat.process_frame(BotStartedSpeakingFrame())
        await strat.process_frame(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.1)
        assert not started
        # ...and "mhm" still doesn't interrupt, but a real word that keeps going does.
        await strat.process_frame(interim("mhm"))
        await strat.process_frame(VADUserStartedSpeakingFrame())
        await strat.process_frame(interim("Maya"))
        await asyncio.sleep(0.1)
        assert started == [True]

    asyncio.run(go())


def test_words_start_a_turn_when_the_agent_is_quiet():
    from pipecat.frames.frames import InterimTranscriptionFrame

    from app.voice.turntaking import BackchannelAwareStartStrategy

    strat = BackchannelAwareStartStrategy()
    started = []

    async def trigger():
        started.append(True)

    strat.trigger_user_turn_started = trigger
    asyncio.run(strat.process_frame(InterimTranscriptionFrame(text="hi", user_id="u", timestamp="0")))
    assert started
