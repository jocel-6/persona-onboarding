"""Voice-layer logic that doesn't need audio: barge-in rules and history repair."""

import asyncio
from types import SimpleNamespace as NS

from app.brain import Brain
from app.config import Settings
from app.state import OnboardingState, Turn
from app.voice.call import _trim_last_agent_turn
from app.voice.turntaking import classify_barge_in
from tests.test_brain import FakeClient


def test_backchannels_are_ignored_but_real_interruptions_stop_the_agent():
    for t in ["mhm", "yeah", "uh-huh right", "okay got it", "yeah yeah", "sure"]:
        assert classify_barge_in(t) == "backchannel", t
    for t in ["wait", "no", "actually", "hold on", "sorry what", "yeah but that's not it", "I think so too"]:
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
    _trim_last_agent_turn(s, "Nice! Once you connect")
    content = s.messages[-1]["content"]
    assert content[0]["text"].startswith("Nice! Once you connect [cut off")
    assert content[1]["type"] == "tool_use"  # tool call still pairs with its pending result
    assert s.transcript[-1].text == "Nice! Once you connect…"
