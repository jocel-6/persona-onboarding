"""#13: real phone calls and texts (Twilio).

Twilio dials the user's number and streams the call's audio to /api/twilio/stream,
where it runs through the same pipeline and brain as the browser call. When the call
ends, the text follow-up lands in the chat and a short recap is sent by SMS.
Off unless TWILIO_* and PUBLIC_BASE_URL are set (Twilio has to reach this server).
"""

from __future__ import annotations

import logging
import re
from xml.sax.saxutils import quoteattr

import httpx

from ..config import Settings
from ..state import OnboardingState

log = logging.getLogger("persona.phone")

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_number(raw: str, default_country: str = "1") -> str | None:
    """'(415) 555-0100' -> '+14155550100'. None if it can't be a real number."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if not digits.startswith("+"):
        digits = "+" + (default_country + digits if len(digits) == 10 else digits)
    return digits if _E164.match(digits) else None


def _api(settings: Settings, path: str) -> str:
    return f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/{path}"


def _auth(settings: Settings) -> tuple[str, str]:
    return settings.twilio_account_sid, settings.twilio_auth_token


async def place_call(settings: Settings, to: str, session_id: str) -> str:
    """Dial the user; Twilio connects the call's audio to our WebSocket. Returns the call SID."""
    ws = settings.public_base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    twiml = (
        "<Response><Connect>"
        f"<Stream url={quoteattr(f'{ws}/api/twilio/stream')}>"
        f"<Parameter name=\"session_id\" value={quoteattr(session_id)} />"
        "</Stream></Connect></Response>"
    )
    async with httpx.AsyncClient(timeout=20, auth=_auth(settings)) as c:
        r = await c.post(_api(settings, "Calls.json"), data={"To": to, "From": settings.twilio_from_number, "Twiml": twiml})
        r.raise_for_status()
        return r.json()["sid"]


async def send_sms(settings: Settings, to: str, body: str) -> None:
    async with httpx.AsyncClient(timeout=20, auth=_auth(settings)) as c:
        r = await c.post(_api(settings, "Messages.json"), data={"To": to, "From": settings.twilio_from_number, "Body": body})
        r.raise_for_status()


def recap_text(state: OnboardingState, link: str | None) -> str:
    """The post-call text: what I got, what I'll do first, what's still missing."""
    name = state.agent_name or "Persona"
    got = []
    if state.user_name:
        got.append(f"You're {state.user_name}")
    if state.help_topic:
        got.append(f"you want help with {state.help_topic}")
    lines = [f"{name} here! Thanks for the call."]
    if got:
        lines.append(", ".join(got) + ".")
    first = state.starter_suggestions[0] if state.starter_suggestions else None
    if first:
        lines.append(f"First up: {first}.")
    missing = [x for x, ok in (("your name", state.user_name), ("Gmail", state.gmail_status == "connected")) if not ok]
    if missing:
        lines.append(f"Still to do: {' and '.join(missing)}, whenever you like.")
    if link:
        lines.append(f"Anything off? Fix it here: {link}")
    return " ".join(lines)[:640]
