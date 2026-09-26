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


def test_accepting_graduation_starts_wrap_up_not_exit():
    s = OnboardingState(agent_name="Iris", user_name="Josie", help_topic="school emails", call_status="in_progress")
    res = orch.apply_tool_call(s, {"graduation_answer": "accepted"})
    assert s.wrapping_up and not s.graduated
    assert {"type": "wrap_up"} in res.ui
    assert "starter_suggestions" in orch.directors_note(s, channel="voice")

    res = orch.apply_tool_call(s, {"starter_suggestions": ["Find permission slips due this week", "  ", 5]})
    assert s.starter_suggestions == ["Find permission slips due this week"]
    assert "anything else" in orch.directors_note(s, channel="voice")

    orch.apply_tool_call(s, {"ready_to_start": True})
    assert s.graduated and s.call_status == "completed"


def test_early_ready_runs_wrap_up_first_but_skip_still_exits():
    s = OnboardingState(user_name="Josie", help_topic="school emails", graduation_offered=True)
    res = orch.apply_tool_call(s, {"ready_to_start": True})
    assert s.wrapping_up and not s.graduated
    assert res.rejected  # forces an immediate follow-up so the wrap-up happens now

    s = OnboardingState(user_name="Josie", help_topic="school emails", graduation_offered=True)
    orch.apply_tool_call(s, {"wants_to_skip": True})
    assert s.graduated


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
    note = orch.directors_note(s, channel="text")
    assert "name you, the assistant: optional" in note and "the call does that" in note  # their name waits for the call

    s.agent_name = "Nova"
    assert "quick two-minute call" in orch.directors_note(s, channel="text")

    s.call_status = "in_progress"
    s.channel = "voice"
    note = orch.directors_note(s, channel="voice")
    # On the call, discovery leads; the name is picked up, never asked on its own.
    assert "Discover what they need" in note and "the call opener asks who" in note
    assert "Graduation: not yet allowed" in note and "20 words max" in note

    s.user_name, s.help_topic = "Maya", "school emails"
    note = orch.directors_note(s, channel="voice")
    assert "framed as a hunch" in note and "allowed, not yet offered" in note  # first: show you get their life
    s.hunch_done = True
    note = orch.directors_note(s, channel="voice")
    assert "show_gmail_button" in note  # then: the Gmail pitch

    s.sentiment = "rushed"
    note = orch.directors_note(s, channel="voice")
    assert "jump in now" in note and "One sentence." in note


def test_name_comes_from_google_then_fold_in():
    s = OnboardingState(agent_name="Iris", call_status="in_progress", channel="voice", help_topic="school emails")
    s.gmail_status, s.google_name = "connected", "Margaret"
    note = orch.directors_note(s, channel="voice")
    assert "Value moment" in note and "Google account says 'Margaret'" in note  # right after connecting
    s.value_moment_done = True
    note = orch.directors_note(s, channel="voice")
    assert "Google account says 'Margaret'" in note and "prefer something else" in note

    s.gmail_status, s.google_name, s.hunch_done = "denied", None, True  # no Gmail: light fold-in ask, never standalone
    assert "Fold a light ask" in orch.directors_note(s, channel="voice")


def test_mood_cues_set_before_the_model_replies():
    s = OnboardingState()
    orch.before_user_turn(s, "can we be quick, I have a meeting in five")
    assert s.sentiment == "rushed"
    orch.before_user_turn(s, "ugh I already told you that")
    assert s.sentiment == "frustrated"
    orch.before_user_turn(s, "hurry")  # rush doesn't downgrade frustration
    assert s.sentiment == "frustrated"

    s = OnboardingState()
    orch.before_user_turn(s, "ugh I keep missing permission slips")  # venting about life, not us
    assert s.sentiment == "neutral"


def test_help_topic_is_discovered_not_asked():
    notes = set()
    for _ in range(12):
        s = OnboardingState(agent_name="Iris", user_name="Maya", call_status="in_progress", channel="voice")
        note = orch.directors_note(s, channel="voice")
        assert "Discover what they need without asking" in note
        assert "need help with" not in note
        notes.add(orch.discovery_angle(s))
    assert len(notes) > 3  # different people get different openers

    s = OnboardingState(agent_name="Iris", user_name="Maya")
    first = orch.discovery_angle(s)
    s.user_turns += 1
    assert orch.discovery_angle(s) != first  # a stalled thread gets a fresh angle


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


def test_name_is_asked_lightly_after_the_first_answer_on_the_call():
    from app.state import Turn

    s = OnboardingState(agent_name="Ollie", call_status="in_progress", channel="voice")
    assert "the call opener asks who" in orch.directors_note(s, channel="voice")  # the opener asks their name
    s.transcript.append(Turn(role="user", text="honestly work has been a lot", channel="voice"))
    note = orch.directors_note(s, channel="voice")
    assert "who am I talking to" in note and "Discover what they need" in note


def test_an_ended_call_cannot_be_revived_by_a_late_connect():
    from app.events import apply_event

    s = OnboardingState(agent_name="Kai", call_status="hung_up", channel="text")
    text, _ = apply_event(s, "call_connected", {})
    assert text is None and s.call_status == "hung_up" and s.channel == "text"
    s.call_status = "ringing"
    text, _ = apply_event(s, "call_connected", {})
    assert text and s.call_status == "in_progress"


def test_after_graduation_it_becomes_the_assistant_and_uses_the_account():
    from app.google import demo_snapshot

    s = OnboardingState(agent_name="Kai", user_name="Jo", help_topic="school emails", graduated=True)
    s.gmail_status, s.gmail, s.account_snapshot = "connected", "demo account", demo_snapshot()
    note = orch.directors_note(s, channel="text")
    assert "you're now their assistant" in note and "Account snapshot" in note
    assert "Wrap up" not in note  # no more goodbyes after the goodbye

    s = OnboardingState(agent_name="Kai", help_topic="school emails", graduated=True)
    assert "Still unknown: their name, Gmail" in orch.directors_note(s, channel="text")


def test_skip_and_gmail_refusal_are_guaranteed_by_code():
    from app import validation as val

    for t in ["can I just skip the setup?", "let me just start using it", "skip all this", "stop asking and let me start"]:
        assert val.wants_to_skip(t), t
    for t in ["can we skip the call and text?", "I skipped lunch", "sure"]:
        assert not val.wants_to_skip(t), t
    for t in ["nah, not connecting my gmail", "I don't want to connect my email", "gmail? no way"]:
        assert val.refuses_gmail(t), t
    assert not val.refuses_gmail("my inbox is a mess")
    assert not val.refuses_gmail("I'm not connecting my email until you tell me what it reads")  # a question
    assert not val.refuses_gmail("what can you see if I connect my gmail? not the bodies, right?")

    s = OnboardingState(agent_name="Juno", call_status="declined", gmail_card_shown=True, gmail_offer_count=1)
    orch.before_user_turn(s, "nah, not doing the gmail thing")
    assert s.gmail_status == "denied" and not s.gmail_card_shown
    orch.before_user_turn(s, "honestly can I just skip the setup")
    assert s.skip_requested and "Let them go right now" in orch.directors_note(s, channel="text")


def test_graduation_is_not_offered_twice_in_a_row():
    s = OnboardingState(agent_name="Kai", user_name="Jo", help_topic="school", user_turns=4, call_status="declined",
                        gmail_status="denied", hunch_done=True)
    orch.apply_tool_call(s, {"offered_graduation": True})
    assert "don't offer again yet" in orch.directors_note(s, channel="text")
    s.user_turns += 3
    assert "you can offer again" in orch.directors_note(s, channel="text")


def test_agent_naming_is_never_phrased_like_asking_their_name():
    note = orch.directors_note(OnboardingState(), channel="text")
    assert "name you, the assistant" in note and "Never 'what should I call you?'" in note


def test_their_own_name_is_never_saved_as_the_agents_name():
    s = OnboardingState()
    res = orch.apply_tool_call(s, {"agent_name": "Dana", "user_name": "Dana"})
    assert s.user_name == "Dana" and s.agent_name is None
    assert any("user's own name" in r for r in res.rejected)


def test_rushed_users_skip_the_wrap_up_ceremony():
    s = OnboardingState(agent_name="Kai", user_name="Dana", help_topic="meetings", wrapping_up=True, sentiment="rushed")
    note = orch.directors_note(s, channel="voice")
    assert "no tips, no questions" in note and "ready_to_start=true" in note


def test_gmail_is_the_next_step_not_deferred():
    s = OnboardingState(agent_name="Kai", user_name="Jo", help_topic="school emails", call_status="in_progress",
                        channel="voice", user_turns=3)
    note = orch.directors_note(s, channel="voice")
    assert "framed as a hunch" in note and "show_gmail_button=true" in note  # the hunch is the reason to connect

    orch.apply_tool_call(s, {"show_gmail_button": True})
    s.hunch_done = True
    note = orch.directors_note(s, channel="voice")
    assert "it's the next step" in note and "Offer to let them jump in" not in note  # no "later" right away
    s.user_turns += orch.GMAIL_GRACE_TURNS
    assert "button stays there" in orch.directors_note(s, channel="voice")  # then they may skip it

    r = OnboardingState(agent_name="Kai", user_name="Jo", help_topic="inbox", sentiment="rushed", user_turns=2,
                        call_status="declined")
    note = orch.directors_note(r, channel="text")
    assert "one-tap shortcut" in note and "picked up later" not in note


def test_later_means_no_while_the_gmail_button_is_up():
    s = OnboardingState(agent_name="Kai", user_name="Dana", help_topic="inbox", gmail_card_shown=True, gmail_offer_count=1)
    orch.before_user_turn(s, "Yeah, do it later though")
    assert s.gmail_status == "denied" and not s.gmail_card_shown
    s = OnboardingState(agent_name="Kai")
    orch.before_user_turn(s, "I'll tell you later")  # no button up: just words
    assert s.gmail_status == "not_connected"


def test_the_call_opener_asks_who_they_are():
    from app.events import apply_event

    s = OnboardingState(agent_name="Milo", call_status="ringing")
    text, _ = apply_event(s, "call_connected", {})
    assert "who you're talking to" in text
    s = OnboardingState(agent_name="Milo", user_name="Jo", call_status="ringing")
    text, _ = apply_event(s, "call_connected", {})
    assert "Greet them by name" in text
