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
    assert "gmail.metadata" in url and "calendar.events.readonly" in url
    assert "gmail.readonly" not in url and "gmail.modify" not in url and "gmail.send" not in url


def test_snapshot_description_is_friendly_and_marks_demo():
    now = datetime(2026, 9, 24, 12, 0).astimezone()
    snap = google.demo_snapshot(now)
    text = google.describe_snapshot(snap, now)
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
