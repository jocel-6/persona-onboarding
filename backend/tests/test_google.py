"""Gmail integration: privacy filter, snapshot formatting, modes, and the OAuth callback (Google mocked)."""

import dataclasses
import json
import os
from datetime import datetime, timedelta

os.environ.setdefault("DB_PATH", ":memory:")

from fastapi.testclient import TestClient  # noqa: E402

from app import google, main, runtime  # noqa: E402
from app import orchestrator as orch  # noqa: E402
from app.events import apply_event  # noqa: E402
from app.state import OnboardingState  # noqa: E402


def test_private_items_are_filtered():
    for t in ["Your bank statement is ready", "Lab results available", "Your verification code: 123456",
              "Appointment with your therapist", "Payment failed", "Security alert"]:
        assert google.is_private(t), t
    for t in ["Field trip permission slip due Friday", "Dentist appointment", "Re: Saturday plans?", "Team standup"]:
        assert not google.is_private(t), t


def test_scopes_are_minimal_and_read_only():
    url = google.auth_url("cid", "http://localhost:8000/api/google/callback", "st8")
    assert "gmail.metadata" in url and "calendar.events" in url  # events: read + add what the user confirms
    assert "gmail.readonly" not in url and "gmail.modify" not in url and "gmail.send" not in url
    assert "auth%2Fcalendar+" not in url and "auth%2Fcalendar%20" not in url  # never full calendar control


def test_snapshot_description_is_friendly_and_marks_demo():
    now = datetime(2026, 9, 24, 19, 0, tzinfo=google.timezone.utc)  # noon in Los Angeles
    snap = google.demo_snapshot(now, "America/Los_Angeles")
    text = google.describe_snapshot(snap, now, "America/Los_Angeles")
    assert "DEMO data" in text
    assert "Dentist appointment (" in text and "9:15am" in text
    assert '"Field trip permission slip due Friday" from Lincoln Elementary' in text
    empty = google.describe_snapshot({"events": [], "emails": [], "errors": ["inbox unavailable (HTTPError)"]}, now)
    assert "calendar is empty" in empty and "don't mention errors" in empty


def test_demo_data_connects_and_feeds_the_value_moment():
    s = OnboardingState(agent_name="Iris", user_name="Maya", help_topic="school emails",
                        call_status="in_progress", channel="voice")
    text, ui = apply_event(s, "gmail_connected", {"demo": True})
    assert s.gmail_status == "connected" and s.gmail_demo and "demo" in text
    note = orch.directors_note(s, channel="voice")
    assert "Value moment" in note and "Account snapshot" in note and "permission slip" in note
    assert "account_snapshot" not in s.public_view()  # never sent to the browser


def test_real_mode_browser_cannot_claim_a_connection(monkeypatch):
    monkeypatch.setattr(runtime, "settings", dataclasses.replace(runtime.settings, gmail_stub=False))
    s = OnboardingState()
    text, _ = apply_event(s, "gmail_connected", {"email": "fake@example.com"})
    assert text is None and s.gmail_status == "not_connected"


def test_oauth_callback_connects_with_snapshot(monkeypatch):
    real = dataclasses.replace(runtime.settings, gmail_stub=False, google_client_id="cid", google_client_secret="sec")
    monkeypatch.setattr(runtime, "settings", real)
    monkeypatch.setattr(main, "settings", real)

    async def exchange(*a):
        return {"access_token": "at", "refresh_token": "rt", "scope": " ".join(google.SCOPES)}

    async def profile(tok):
        return {"email": "margaret@gmail.com", "given_name": "Margaret", "name": "Margaret Chen"}

    async def snapshot(tok):
        soon = (datetime.now().astimezone() + timedelta(days=1)).isoformat()
        return {"events": [{"title": "Dentist appointment", "start": soon}], "emails": [], "errors": [], "demo": False}

    monkeypatch.setattr(google, "exchange_code", exchange)
    monkeypatch.setattr(google, "fetch_profile", profile)
    monkeypatch.setattr(google, "fetch_snapshot", snapshot)

    c = TestClient(main.app)
    sid = c.post("/api/sessions").json()["state"]["session_id"]
    r = c.get(f"/api/google/start?session_id={sid}", follow_redirects=False)
    assert r.status_code in (302, 307) and "accounts.google.com" in r.headers["location"]
    state_token = r.headers["location"].split("state=")[1].split("&")[0]

    page = c.get(f"/api/google/callback?state={state_token}&code=abc").text
    assert '"ok": true' in page
    st = runtime.store.get(sid)
    assert st.gmail == "margaret@gmail.com" and st.google_name == "Margaret" and st.gmail_status == "connected"
    assert runtime.store.get_tokens(sid)["refresh_token"] == "rt"
    browser_view = json.dumps(c.get(f"/api/sessions/{sid}").json())
    assert "account_snapshot" not in browser_view and "Dentist" not in browser_view and "rt" not in json.loads(browser_view)["state"]

    # Replayed or forged callback states are rejected.
    assert '"reason": "expired"' in c.get(f"/api/google/callback?state={state_token}&code=abc").text

    # Denied on the consent screen.
    r = c.get(f"/api/google/start?session_id={sid}", follow_redirects=False)
    tok2 = r.headers["location"].split("state=")[1].split("&")[0]
    assert '"reason": "denied"' in c.get(f"/api/google/callback?state={tok2}&error=access_denied").text
    assert c.get(f"/api/sessions/{sid}").json()["state"]["google_result"] == "denied"  # what the browser polls


def test_unticked_permissions_are_not_connected(monkeypatch):
    real = dataclasses.replace(runtime.settings, gmail_stub=False, google_client_id="cid", google_client_secret="sec")
    monkeypatch.setattr(runtime, "settings", real)
    monkeypatch.setattr(main, "settings", real)

    async def exchange(*a):
        return {"access_token": "at", "scope": "openid email profile"}  # calendar + gmail unticked

    revoked = []

    async def revoke(t):
        revoked.append(t)

    monkeypatch.setattr(google, "exchange_code", exchange)
    monkeypatch.setattr(google, "revoke", revoke)
    c = TestClient(main.app)
    sid = c.post("/api/sessions").json()["state"]["session_id"]
    loc = c.get(f"/api/google/start?session_id={sid}", follow_redirects=False).headers["location"]
    tok = loc.split("state=")[1].split("&")[0]
    assert '"reason": "missing_scopes"' in c.get(f"/api/google/callback?state={tok}&code=x").text
    assert revoked == ["at"] and runtime.store.get_tokens(sid) is None


def test_event_times_are_shown_in_the_users_timezone_not_the_servers():
    utc_event = {"events": [{"title": "Standup", "start": "2026-09-25T16:30:00Z"}], "emails": []}
    now = datetime(2026, 9, 24, 19, 0, tzinfo=google.timezone.utc)
    assert "Standup (tomorrow 9:30am)" in google.describe_snapshot(utc_event, now, "America/Los_Angeles")
    assert "Standup (tomorrow 1:30am)" in google.describe_snapshot(utc_event, now, "Asia/Tokyo")  # already the 25th there
    assert "(tomorrow 4:30pm)" in google.describe_snapshot(utc_event, now, "Not/AZone")  # bad tz -> UTC, no crash


# ---------------------------------------------------------------------------
# Adding to the calendar: propose -> confirm card -> only the user's tap writes
# ---------------------------------------------------------------------------


def test_event_proposals_are_validated_in_the_users_timezone():
    from datetime import datetime, timezone

    from app import validation as val

    now = datetime(2026, 9, 25, 17, 0, tzinfo=timezone.utc)  # 10am in Los Angeles
    ev, err = val.check_event({"title": "Park picnic", "start": "2026-09-26T14:00"}, "America/Los_Angeles", now)
    assert err is None and ev["start"] == "2026-09-26T14:00:00-07:00" and ev["when"] == "Sat Sep 26, 2–3pm"
    assert val.check_event({"title": "Late", "start": "2026-09-24T09:00"}, "America/Los_Angeles", now)[1]
    assert val.check_event({"title": "Far", "start": "2028-01-01T09:00"}, "America/Los_Angeles", now)[1]
    assert val.check_event({"title": "x", "start": "tomorrow at 2"}, "America/Los_Angeles", now)[1]
    all_day, _ = val.check_event({"title": "Trip", "start": "2026-10-01", "days": 3}, "America/Los_Angeles", now)
    assert all_day["all_day"] and all_day["when"] == "Thu Oct 1 to Sat Oct 3, all day"


def test_the_model_can_only_propose_never_add():
    s = OnboardingState(agent_name="Wren", user_tz="America/Los_Angeles")
    res = orch.propose_event(s, {"title": "Park picnic", "start": "2099-01-01T14:00"})
    assert res.rejected and s.pending_event is None  # needs Gmail/calendar connected first

    s.gmail_status = "connected"
    res = orch.propose_event(s, {"title": "Park picnic", "start": "2026-12-01T14:00"})
    assert s.pending_event["title"] == "Park picnic" and not s.added_events
    assert res.ui[0]["type"] == "confirm_event" and "Don't say it's added" in res.tool_result_text()
    assert "confirm card for 'Park picnic'" in orch.directors_note(s, channel="voice")
    assert "Now: " in orch.directors_note(s, channel="voice")


def test_tapping_add_writes_the_event_and_not_now_doesnt(monkeypatch):
    real = dataclasses.replace(runtime.settings, gmail_stub=False, google_client_id="cid", google_client_secret="sec")
    monkeypatch.setattr(runtime, "settings", real)
    monkeypatch.setattr(main, "settings", real)
    inserted = []

    async def fake_insert(token, ev):
        inserted.append((token, ev["title"]))
        return {"id": "evt1"}

    monkeypatch.setattr(google, "insert_event", fake_insert)

    from tests.test_brain import FakeClient

    main.brain.client = FakeClient([("Done, it's on there.", None, "end_turn")] * 4)
    c = TestClient(main.app)
    sid = c.post("/api/sessions").json()["state"]["session_id"]

    def with_pending(scope: str):
        st = runtime.store.get(sid)
        st.gmail_status, st.channel = "connected", "text"
        st.pending_event = {"title": "Park picnic", "start": "2026-12-01T14:00:00-08:00",
                            "end": "2026-12-01T15:00:00-08:00", "tz": "America/Los_Angeles",
                            "all_day": False, "location": None, "when": "Tue Dec 1, 2–3pm"}
        runtime.store.save(st)
        runtime.store.save_tokens(sid, {"access_token": "at", "refresh_token": "rt", "scope": scope,
                                        "expires_at": 9e12})

    # Connected before calendar writes existed: asks them to reconnect, writes nothing.
    with_pending("openid https://www.googleapis.com/auth/calendar.events.readonly")
    c.post(f"/api/sessions/{sid}/events", json={"type": "event_confirmed"})
    st = runtime.store.get(sid)
    assert not inserted and st.pending_event and st.gmail_card_shown

    # With the write permission, the user's tap adds it.
    with_pending(" ".join(google.SCOPES))
    c.post(f"/api/sessions/{sid}/events", json={"type": "event_confirmed"})
    st = runtime.store.get(sid)
    assert inserted == [("at", "Park picnic")] and st.pending_event is None
    assert st.added_events[0]["title"] == "Park picnic" and "Added to calendar" in st.transcript[-2].text

    # Not now: nothing written.
    with_pending(" ".join(google.SCOPES))
    c.post(f"/api/sessions/{sid}/events", json={"type": "event_cancelled"})
    assert len(inserted) == 1 and runtime.store.get(sid).pending_event is None
