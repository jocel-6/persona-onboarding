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
