"""HTTP-level smoke test: session lifecycle, SSE turns, and call events (fake model)."""

import json
import os

os.environ["DB_PATH"] = ":memory:"

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from tests.test_brain import FakeClient  # noqa: E402


def sse(resp):
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def test_full_flow_over_http():
    fake = FakeClient(
        [
            ("Juno, love it! Up for a quick call, or keep texting?", {"agent_name": "Juno"}, "tool_use"),
            ("Hey, it's Juno! Who am I talking to?", None, "end_turn"),
            ("Nice to meet you, Maya!", {"user_name": "Maya"}, "tool_use"),
            ("Looks like we got cut off! Want me to call back, or finish here?", None, "end_turn"),
            ("Welcome back, Maya!", None, "end_turn"),
        ]
    )
    main.brain.client = fake
    c = TestClient(main.app)

    created = c.post("/api/sessions").json()
    sid = created["state"]["session_id"]
    assert created["ui"][0]["type"] == "name_suggestions"
    assert created["state"]["transcript"][0]["role"] == "agent"
    assert "messages" not in created["state"]  # raw history never leaves the server

    evs = sse(c.post(f"/api/sessions/{sid}/messages", json={"text": "Juno"}))
    assert {"type": "ui", "ui": {"type": "show_call_offer"}} in evs
    assert evs[-2]["state"]["agent_name"] == "Juno" and evs[-1] == {"type": "end"}

    evs = sse(c.post(f"/api/sessions/{sid}/events", json={"type": "call_accepted"}))
    assert {"type": "ui", "ui": {"type": "ringing"}} in evs
    assert len(fake.calls) == 1  # ringing needs no model turn

    evs = sse(c.post(f"/api/sessions/{sid}/events", json={"type": "call_connected"}))
    assert evs[-2]["state"]["channel"] == "voice"

    sse(c.post(f"/api/sessions/{sid}/messages", json={"text": "I'm Maya"}))
    assert "[voice] I'm Maya" in json.dumps(fake.calls[-1]["messages"][-1])

    evs = sse(c.post(f"/api/sessions/{sid}/events", json={"type": "hangup"}))
    st = evs[-2]["state"]
    assert st["call_status"] == "hung_up" and st["channel"] == "text" and st["user_name"] == "Maya"
    assert {"type": "ui", "ui": {"type": "show_callback"}} in evs

    evs = sse(c.post(f"/api/sessions/{sid}/events", json={"type": "resumed"}))
    assert evs[-3]["reply"] == "Welcome back, Maya!"

    assert c.get(f"/api/sessions/{sid}").json()["state"]["user_name"] == "Maya"
    assert c.get("/api/sessions/nope").status_code == 404


def test_recap_edit_validates_and_tells_the_model():
    fake = FakeClient([("Sure thing, Sam.", None, "end_turn")])
    main.brain.client = fake
    c = TestClient(main.app)
    sid = c.post("/api/sessions").json()["state"]["session_id"]

    r = c.post(f"/api/sessions/{sid}/fields", json={"field": "user_name", "value": "my name is Sam"})
    assert r.status_code == 422 and "name" in r.json()["detail"]

    st = c.post(f"/api/sessions/{sid}/fields", json={"field": "user_name", "value": "Sam"}).json()["state"]
    assert st["user_name"] == "Sam" and st["transcript"][-1]["text"] == "Updated name: Sam"
    assert c.post(f"/api/sessions/{sid}/fields", json={"field": "gmail", "value": "x@y.com"}).status_code == 422

    sse(c.post(f"/api/sessions/{sid}/messages", json={"text": "cool"}))
    sent = json.dumps(fake.calls[-1]["messages"][-1])
    assert "edited their name in the recap" in sent  # the model hears about it once


def test_voice_picker_only_accepts_offered_voices(monkeypatch):
    import dataclasses

    from app import runtime

    from app.config import BAKEOFF_VOICES

    base = dataclasses.replace(runtime.settings, tts_provider="cartesia", cartesia_api_key="k", voice_choices_json="[]")
    assert base.voice_choices() == []  # the default: one voice for everyone, no picker
    s = dataclasses.replace(base, voice_choices_json=BAKEOFF_VOICES)
    monkeypatch.setattr(runtime, "settings", s)
    monkeypatch.setattr(main, "settings", s)
    c = TestClient(main.app)
    created = c.post("/api/sessions").json()
    choices = created["config"]["voice_choices"]
    assert len(choices) == 3 and all(v["name"] for v in choices)
    sid = created["state"]["session_id"]
    assert c.post(f"/api/sessions/{sid}/voice", json={"voice_id": "not-a-voice"}).status_code == 422
    st = c.post(f"/api/sessions/{sid}/voice", json={"voice_id": choices[1]["id"]}).json()["state"]
    assert st["voice_id"] == choices[1]["id"] and st["transcript"][-1]["text"] == f"Voice: {choices[1]['name']}"
    assert c.get("/api/voices/not-a-voice/sample?name=Wren").status_code == 404


def test_export_has_everything_but_never_tokens():
    from app import runtime

    c = TestClient(main.app)
    sid = c.post("/api/sessions").json()["state"]["session_id"]
    runtime.store.save_tokens(sid, {"access_token": "secret-at", "refresh_token": "secret-rt"})
    r = c.get(f"/api/sessions/{sid}/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    body = r.text
    assert "secret-at" not in body and "secret-rt" not in body
    data = json.loads(body)
    assert data["google_access_token_stored"] is True and data["conversation"][0]["role"] == "agent"


def test_metrics_funnel_latency_and_cost(monkeypatch):
    import dataclasses

    from app import runtime

    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, metrics_token=""))  # ignore a local .env

    fake = FakeClient([("Nice to meet you!", {"user_name": "Maya", "help_topic": "school emails"}, "tool_use")])
    main.brain.client = fake
    c = TestClient(main.app)
    sid = c.post("/api/sessions").json()["state"]["session_id"]
    sse(c.post(f"/api/sessions/{sid}/messages", json={"text": "I'm Maya, school emails are burying me"}))
    runtime.store.record_turn(session_id=sid, channel="voice", model="claude-haiku-4-5", ttft_ms=500, total_ms=900,
                              input_tokens=100, output_tokens=20, cost_usd=0.0002)
    runtime.store.record_first_audio(sid, 980, "reply")

    m = c.get("/api/metrics?days=1").json()
    steps = {f["step"]: f["count"] for f in m["funnel"]}
    assert steps["Opened Persona"] >= 1 and steps["Shared what they need"] >= 1
    assert m["kpis"]["voice_first_audio_p50"] == 980 and m["kpis"]["turns"] >= 2
    assert sum(b["count"] for b in m["voice_latency_hist"]) >= 1
    assert any(r["model"] == "claude-haiku-4-5" for r in m["by_model"])


def test_phone_calls_are_off_until_configured_and_numbers_are_cleaned():
    from app.state import OnboardingState
    from app.voice.phone import normalize_number, recap_text

    assert normalize_number("(415) 555-0100") == "+14155550100"
    assert normalize_number("+44 20 7946 0958") == "+442079460958"
    assert normalize_number("call me maybe") is None

    c = TestClient(main.app)
    created = c.post("/api/sessions").json()
    assert created["config"]["phone"] is False  # no Twilio configured in tests
    r = c.post(f"/api/sessions/{created['state']['session_id']}/phone-call", json={"phone": "415 555 0100"})
    assert r.status_code == 503

    s = OnboardingState(agent_name="Juno", user_name="Maya", help_topic="school emails",
                        starter_suggestions=["Find permission slips due this week"])
    text = recap_text(s, "https://persona.example")
    assert text.startswith("Juno here!") and "school emails" in text and "Still to do: Gmail" in text
    assert "https://persona.example" in text and len(text) <= 640


def test_feedback_is_saved_and_conversations_need_the_token(monkeypatch):
    import dataclasses

    from app import runtime

    c = TestClient(main.app)
    sid = c.post("/api/sessions").json()["state"]["session_id"]
    st = c.post(f"/api/sessions/{sid}/feedback", json={"rating": "up", "text": "  felt natural  "}).json()["state"]
    assert st["feedback_rating"] == "up" and st["feedback_text"] == "felt natural"
    assert c.post(f"/api/sessions/{sid}/feedback", json={"rating": "meh"}).status_code == 422

    s = dataclasses.replace(runtime.settings, metrics_token="")
    monkeypatch.setattr(main, "settings", s)
    assert c.get("/api/conversations").status_code == 401  # off when no token is configured
    monkeypatch.setattr(main, "settings", dataclasses.replace(s, metrics_token="t0k"))
    assert c.get("/api/conversations?token=wrong").status_code == 401
    runtime.store.save_tokens(sid, {"access_token": "secret-at"})
    state = runtime.store.get(sid)
    state.user_turns = 1
    runtime.store.save(state)
    body = c.get("/api/conversations?token=t0k").text
    assert sid[:8] in body and "felt natural" in body and "secret-at" not in body
