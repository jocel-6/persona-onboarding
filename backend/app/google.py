"""Google sign-in and the read-only account snapshot behind the value moment.

Privacy by construction:
  * Scopes: sign-in, read-only calendar events, and gmail.metadata, which can read
    headers (subject, sender) but cannot open message bodies at all.
  * One snapshot right after connecting: the next ~10 calendar events and ~20 recent
    inbox subject lines. Anything that looks medical, financial, or otherwise private
    is dropped before the model ever sees it.
  * Tokens live only in a server-side table (never in session state, the browser,
    or the model) and are revoked and deleted on disconnect.
  * Nothing from the inbox is logged.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

log = logging.getLogger("persona.google")

SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/gmail.metadata",
]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

MAX_EVENTS = 10
MAX_SUBJECTS = 20

# Things the agent should never bring up, even as a helpful suggestion.
_PRIVATE_RE = re.compile(
    r"\b(diagnos\w*|lab (results?|work)|test results?|biopsy|therap(y|ist)|psychiatr\w*|counsel+ing|prescription|"
    r"pharmacy|std|hiv|pregnan\w*|fertility|ivf|rehab|oncolog\w*|chemo\w*|surgery|"
    r"bank statement|statement (is )?ready|overdra\w*|loan|mortgage|credit (score|report|card)|debt|collections?|"
    r"past due|payment (failed|declined)|salary|payroll|tax(es)? (return|notice)|irs|"
    r"password|verification code|security (alert|code)|one[- ]time (code|passcode)|otp|2fa|sign[- ]in attempt|"
    r"divorce|lawyer|attorney|custody|court|police|"
    r"dating|match\.com|tinder|hinge|bumble)\b",
    re.IGNORECASE,
)


def is_private(text: str) -> bool:
    return bool(_PRIVATE_RE.search(text or ""))


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def new_state_token() -> str:
    return secrets.token_urlsafe(24)


def auth_url(client_id: str, redirect_uri: str, state: str, login_hint: str | None = None) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        r.raise_for_status()
        tok = r.json()
    tok["expires_at"] = time.time() + float(tok.get("expires_in", 3600))
    return tok


def granted_all_scopes(token: dict[str, Any]) -> list[str]:
    """Scopes the user unticked on the consent screen (Google lets them)."""
    granted = set((token.get("scope") or "").split())
    return [s for s in SCOPES if s.startswith("https://") and s not in granted]


async def revoke(token: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(REVOKE_URL, params={"token": token})
    except httpx.HTTPError:
        log.warning("token revoke failed (continuing; tokens are deleted locally anyway)")


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


async def _get(c: httpx.AsyncClient, url: str, token: str, params: dict | None = None) -> dict[str, Any]:
    """GET with one retry (plan: retry once, then continue without the data)."""
    for attempt in (1, 2):
        try:
            r = await c.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError:
            if attempt == 2:
                raise
            await asyncio.sleep(0.4)
    return {}


async def fetch_profile(access_token: str) -> dict[str, str]:
    async with httpx.AsyncClient(timeout=15) as c:
        info = await _get(c, USERINFO_URL, access_token)
    return {"email": info.get("email", ""), "given_name": info.get("given_name", ""), "name": info.get("name", "")}


async def fetch_snapshot(access_token: str) -> dict[str, Any]:
    """Next ~10 events and ~20 inbox subjects, private items removed. Never raises."""
    snap: dict[str, Any] = {"events": [], "emails": [], "errors": [], "demo": False}
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            data = await _get(
                c,
                CALENDAR_URL,
                access_token,
                {
                    "timeMin": datetime.now(timezone.utc).isoformat(),
                    "timeMax": (datetime.now(timezone.utc) + timedelta(days=21)).isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": MAX_EVENTS * 2,
                },
            )
            for ev in data.get("items", []):
                title = (ev.get("summary") or "").strip()
                start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
                if title and start and not is_private(title):
                    snap["events"].append({"title": title[:80], "start": start})
                if len(snap["events"]) >= MAX_EVENTS:
                    break
        except httpx.HTTPError as e:
            snap["errors"].append(f"calendar unavailable ({type(e).__name__})")

        try:
            listing = await _get(c, GMAIL_URL, access_token, {"labelIds": "INBOX", "maxResults": MAX_SUBJECTS * 2})
            ids = [m["id"] for m in listing.get("messages", [])]

            async def one(mid: str) -> dict[str, str] | None:
                try:
                    msg = await _get(
                        c, f"{GMAIL_URL}/{mid}", access_token,
                        {"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]},
                    )
                except httpx.HTTPError:
                    return None
                h = {x["name"].lower(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
                subject, sender = h.get("subject", "").strip(), _sender_name(h.get("from", ""))
                if not subject or is_private(subject) or is_private(sender):
                    return None
                return {"subject": subject[:100], "from": sender[:40]}

            results = await asyncio.gather(*(one(i) for i in ids))
            snap["emails"] = [r for r in results if r][:MAX_SUBJECTS]
        except httpx.HTTPError as e:
            snap["errors"].append(f"inbox unavailable ({type(e).__name__})")
    return snap


def _sender_name(from_header: str) -> str:
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', from_header)
    return (m.group(1) if m else from_header.split("@")[0]).strip()


# ---------------------------------------------------------------------------
# Demo data (off by default; only for reviewers who can't sign in as a test user)
# ---------------------------------------------------------------------------


def user_zone(tz_name: str | None) -> timezone | ZoneInfo:
    """The user's timezone from their browser; UTC if unknown or invalid."""
    try:
        return ZoneInfo(tz_name) if tz_name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def demo_snapshot(now: datetime | None = None, tz_name: str | None = None) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(user_zone(tz_name))

    def at(days: int, hour: int, minute: int = 0) -> str:
        d = (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return d.isoformat()

    return {
        "demo": True,
        "errors": [],
        "events": [
            {"title": "Team standup", "start": at(1, 9, 30)},
            {"title": "Dentist appointment", "start": at(2, 9, 15)},
            {"title": "Parent-teacher conference", "start": at(3, 16, 0)},
            {"title": "Soccer practice pickup", "start": at(3, 17, 30)},
            {"title": "Dinner with Sam", "start": at(4, 19, 0)},
            {"title": "Quarterly planning", "start": at(6, 13, 0)},
        ],
        "emails": [
            {"subject": "Field trip permission slip due Friday", "from": "Lincoln Elementary"},
            {"subject": "Re: Saturday plans?", "from": "Jordan"},
            {"subject": "Your order has shipped", "from": "Hollow Pines Outfitters"},
            {"subject": "Book club: this month's pick", "from": "Priya"},
            {"subject": "Volunteer sign-up for the bake sale", "from": "PTA"},
            {"subject": "Draft deck for Thursday", "from": "Alex (work)"},
            {"subject": "Reminder: picture day next week", "from": "Lincoln Elementary"},
        ],
    }


# ---------------------------------------------------------------------------
# Formatting for the director's note
# ---------------------------------------------------------------------------


def _when(start: str, now: datetime) -> str:
    """'tomorrow 9:15am' in the user's timezone (now carries it), however the event was stored."""
    try:
        if len(start) == 10:  # all-day event: YYYY-MM-DD (no timezone by definition)
            label, time_part = datetime.fromisoformat(start).date(), ""
        else:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=now.tzinfo)
            dt = dt.astimezone(now.tzinfo)
            label = dt.date()
            time_part = " " + dt.strftime("%-I:%M%p").lower().replace(":00", "")
        local_today = now.date()
        delta = (label - local_today).days
        day = "today" if delta == 0 else "tomorrow" if delta == 1 else label.strftime("%A %b %-d")
        return day + time_part
    except ValueError:
        return start


def describe_snapshot(snap: dict[str, Any] | None, now: datetime | None = None, tz_name: str | None = None) -> str:
    """Compact, model-facing summary. Titles and subjects only; never bodies."""
    if not snap:
        return "no account data"
    now = now or datetime.now(timezone.utc)
    if tz_name:
        now = now.astimezone(user_zone(tz_name))
    lines = []
    if snap.get("demo"):
        lines.append("(DEMO data, not their real account: say so if you mention something from it)")
    if snap.get("events"):
        lines.append("Upcoming: " + "; ".join(f"{e['title']} ({_when(e['start'], now)})" for e in snap["events"]))
    else:
        lines.append("Upcoming: calendar is empty")
    if snap.get("emails"):
        lines.append("Recent inbox subjects: " + "; ".join(f"\"{e['subject']}\" from {e['from']}" for e in snap["emails"]))
    else:
        lines.append("Recent inbox: nothing usable")
    if snap.get("errors"):
        lines.append("Couldn't load: " + ", ".join(snap["errors"]) + " (don't mention errors; just don't use that part)")
    return "\n".join(lines)
