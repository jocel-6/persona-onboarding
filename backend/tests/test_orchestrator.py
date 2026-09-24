from app import orchestrator as orch
from app import validation as v
from app.state import OnboardingState


def test_name_validation():
    assert v.check_person_name("Maya")[0] == "Maya"
    assert v.check_person_name("  'Mary-Jane O'Neil' ")[0] == "Mary-Jane O'Neil"
    assert v.check_person_name("José")[0] == "José"
    assert v.check_person_name("my name is Maya")[0] is None
    assert v.check_person_name("Maya and I need help with emails")[0] is None
    assert v.check_person_name("R2D2")[0] == "R2D2"  # joke names are played along with
    assert v.check_person_name("maya@gmail.com")[0] is None
    assert v.check_email("Foo.Bar@Gmail.com")[0] == "foo.bar@gmail.com"
    assert v.check_email("not an email")[0] is None


def test_graduation_rule():
    s = OnboardingState()
    assert not orch.graduation_allowed(s)
    s.help_topic = "school emails"
    assert not orch.graduation_allowed(s)  # needs name or gmail too
    s.user_name = "Maya"
    assert orch.graduation_allowed(s)
    s.user_name = None
    s.gmail = "maya@gmail.com"
    assert orch.graduation_allowed(s)


def test_multi_slot_extraction_and_correction():
    s = OnboardingState(agent_name="Nova", call_status="in_progress", channel="voice")
    res = orch.apply_tool_call(s, {"user_name": "Maya", "help_topic": "keeping up with kid's school emails"})
    assert res.progressed and not res.rejected
    assert s.user_name == "Maya" and s.help_topic.startswith("keeping up")

    res = orch.apply_tool_call(s, {"user_name": "Mia", "correction_of": "user_name", "sentiment": "rushed"})
    assert s.user_name == "Mia"
    assert "user_name" in s.corrected
    assert s.sentiment == "rushed"


def test_rejected_value_is_reported_not_saved():
    s = OnboardingState()
    res = orch.apply_tool_call(s, {"user_name": "my name is actually Maya"})
    assert s.user_name is None
    assert res.rejected and "REJECTED" in res.tool_result_text()


def test_skip_graduates_immediately_with_defaults():
    s = OnboardingState()
    res = orch.apply_tool_call(s, {"wants_to_skip": True})
    assert s.graduated and s.agent_name == "Nova" and s.agent_name_defaulted
    assert {"type": "graduated"} in res.ui


def test_graduation_decline_cooldown():
    s = OnboardingState(user_name="Sam", help_topic="inbox", user_turns=5)
    orch.apply_tool_call(s, {"graduation_answer": "declined"})
    assert orch.graduation_cooling_down(s)
    s.user_turns += 3
    assert not orch.graduation_cooling_down(s)


def test_call_flow_via_words():
    s = OnboardingState(agent_name="Kai", call_status="offered")
    res = orch.apply_tool_call(s, {"wants_call": False})
    assert s.call_status == "declined" and s.channel == "text"
    assert {"type": "hide_call_offer"} in res.ui

    s = OnboardingState(agent_name="Kai", call_status="in_progress", channel="voice")
    res = orch.apply_tool_call(s, {"wants_text": True})
    assert s.channel == "text" and s.call_status == "completed"


def test_director_note_priorities():
    s = OnboardingState()
    assert "call you" in orch.directors_note(s, channel="text")

    s.agent_name = "Nova"
    assert "quick two-minute call" in orch.directors_note(s, channel="text")

    s.call_status = "in_progress"
    s.channel = "voice"
    note = orch.directors_note(s, channel="voice")
    assert "Learn what to call them" in note and "Graduation: not yet allowed" in note

    s.user_name, s.help_topic = "Maya", "school emails"
    note = orch.directors_note(s, channel="voice")
    assert "show_gmail_button" in note and "allowed, not yet offered" in note

    s.sentiment = "rushed"
    note = orch.directors_note(s, channel="voice")
    assert "jump in now" in note and "One sentence." in note


def test_director_note_frustrated_and_stall():
    s = OnboardingState(agent_name="Nova", call_status="in_progress", channel="voice", sentiment="frustrated")
    note = orch.directors_note(s, channel="voice")
    assert "frustrated" in note and "switch to text or skip" in note

    s.sentiment = "neutral"
    s.turns_since_progress = 3
    assert "haven't moved things forward" in orch.directors_note(s, channel="voice")


def test_short_answer_streak_resets():
    s = OnboardingState()
    orch.before_user_turn(s, "ok")
    orch.before_user_turn(s, "sure")
    assert s.short_answer_streak == 2
    orch.before_user_turn(s, "honestly I just need help keeping up with email")
    assert s.short_answer_streak == 0
