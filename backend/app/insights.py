"""Persona noticed: problems the user didn't know they had, found in their calendar and inbox.

Detection is plain code: free per user, instant, deterministic, and testable. The model's
job is the human part: pick the most surprising, relevant finding for *this* person and
say it like a friend would, then offer the fix. Each finding carries its evidence and,
when there's an obvious fix, an action the UI can offer in one tap.

Detectors:
  conflict   two events overlap
  tight      back to back with no breathing room
  packed     a day with a lot going on
  deadline   a due date in the inbox that isn't on the calendar
  prep       an email that's clearly material for an upcoming meeting
  reply      a person waiting on an answer
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .google import user_zone

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

_DEADLINE_RE = re.compile(
    r"\b(due|deadline|rsvp|sign[- ]?ups?|permission slip|expires?|last day|closes|submit|register)\b", re.I
)
_PREP_RE = re.compile(r"\b(deck|slides|draft|agenda|notes|doc|proposal|brief|pre-?read|materials)\b", re.I)
_MEETING_RE = re.compile(r"\b(planning|review|meeting|sync|interview|presentation|pitch|board|offsite|1:1)\b", re.I)
_ASK_RE = re.compile(r"\?|\b(plans|thoughts|are you|can you|could you|free|available|let me know|still on)\b", re.I)
_ORG_RE = re.compile(
    r"\b(no-?reply|noreply|team|school|elementary|academy|pta|inc|llc|store|shop|outfitters|news|support|billing|"
    r"notifications?|updates?|club)\b",
    re.I,
)
_STOP = {"the", "a", "an", "for", "and", "of", "to", "with", "your", "my", "on", "at", "in", "re", "fwd", "this", "is"}


@dataclass
class Insight:
    kind: str
    headline: str  # what the card shows
    detail: str  # why it matters, in a sentence
    score: float
    when: datetime | None = None
    action: dict[str, Any] | None = None  # e.g. {"type": "add_event", "event": {...}}
    id: str = field(default="")

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["when"] = self.when.isoformat() if self.when else None
        return d


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse(ts: str | None, tz) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if len(ts) == 10:  # all-day date
        return datetime.combine(dt.date(), datetime.min.time(), tzinfo=tz)
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(tz)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in _STOP and len(w) > 2}


def _day_label(d: date, today: date) -> str:
    delta = (d - today).days
    return "today" if delta == 0 else "tomorrow" if delta == 1 else d.strftime("%A")


def _time(dt: datetime) -> str:
    return dt.strftime("%-I:%M%p").lower().replace(":00", "")


def resolve_date(text: str, today: date) -> date | None:
    """'due Friday' / 'tomorrow' / 'Oct 3' / '10/3' -> the next such date on or after today."""
    t = text.lower()
    if re.search(r"\btoday\b|\btonight\b|\beod\b", t):
        return today
    if re.search(r"\btomorrow\b", t):
        return today + timedelta(days=1)
    for i, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b|\b{name[:3]}\b", t):
            return today + timedelta(days=(i - today.weekday()) % 7)
    m = re.search(r"\b(" + "|".join(MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\b", t)
    if m:
        month, day = MONTHS.index(m.group(1)) + 1, int(m.group(2))
    else:
        m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", t)
        if not m:
            return None
        month, day = int(m.group(1)), int(m.group(2))
    try:
        d = date(today.year, month, day)
    except ValueError:
        return None
    return d if d >= today else date(today.year + 1, month, day) if (today - d).days > 60 else None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def find_insights(
    snapshot: dict[str, Any] | None,
    *,
    tz_name: str | None = None,
    help_topic: str | None = None,
    now: datetime | None = None,
    limit: int = 5,
) -> list[Insight]:
    if not snapshot:
        return []
    tz = user_zone(tz_name)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = now.date()
    topic = _words(help_topic or "")

    events = []
    for e in snapshot.get("events", []):
        start = _parse(e.get("start"), tz)
        if not start or start < now - timedelta(hours=1):
            continue
        all_day = len(e.get("start", "")) == 10
        end = _parse(e.get("end"), tz) or (start + timedelta(days=1) if all_day else start + timedelta(hours=1))
        events.append({"title": e["title"], "start": start, "end": end, "all_day": all_day})
    events.sort(key=lambda e: e["start"])
    timed = [e for e in events if not e["all_day"]]
    emails = snapshot.get("emails", [])

    out: list[Insight] = []

    def soon_bonus(dt: datetime | None) -> float:
        if not dt:
            return 0
        days = max(0.0, (dt - now).total_seconds() / 86400)
        return max(0.0, 20 - 3 * days)  # today +20 ... a week out ~0

    def topic_bonus(*texts: str) -> float:
        return 15 if topic and topic & _words(" ".join(texts)) else 0

    # conflicts and back-to-backs
    conflicted: set[int] = set()
    for i, a in enumerate(timed):
        for b in timed[i + 1:]:
            if b["start"] >= a["end"] + timedelta(minutes=10) or b["start"].date() != a["start"].date():
                continue
            day = _day_label(a["start"].date(), today)
            if b["start"] < a["end"]:
                if id(a) in conflicted or id(b) in conflicted:
                    continue  # one headline per clash is plenty
                overlap = int((a["end"] - b["start"]).total_seconds() // 60)
                conflicted.update({id(a), id(b)})
                out.append(Insight(
                    kind="conflict",
                    headline=f"{a['title']} runs into {b['title']} {day}",
                    detail=f"{a['title']} ends at {_time(a['end'])} but {b['title']} starts at {_time(b['start'])}: "
                           f"a {overlap}-minute overlap.",
                    score=90 + soon_bonus(a["start"]) + topic_bonus(a["title"], b["title"]),
                    when=b["start"],
                ))
            elif (a["end"] - a["start"]) >= timedelta(minutes=30) and (b["end"] - b["start"]) >= timedelta(minutes=30):
                out.append(Insight(
                    kind="tight",
                    headline=f"No gap between {a['title']} and {b['title']} {day}",
                    detail=f"{a['title']} ends at {_time(a['end'])} and {b['title']} starts right after, "
                           "with no time to get there, eat, or reset.",
                    score=45 + soon_bonus(b["start"]) + topic_bonus(a["title"], b["title"]),
                    when=b["start"],
                ))

    # packed days
    by_day: dict[date, list[dict]] = {}
    for e in timed:
        by_day.setdefault(e["start"].date(), []).append(e)
    busiest = max(by_day.items(), key=lambda kv: len(kv[1]), default=None)
    if busiest and len(busiest[1]) >= 4:
        d, evs = busiest
        span = f"{_time(evs[0]['start'])} to {_time(max(e['end'] for e in evs))}"
        out.append(Insight(
            kind="packed",
            headline=f"{_day_label(d, today).capitalize()} is packed: {len(evs)} things, {span}",
            detail="Worth protecting a break, or moving whatever's flexible.",
            score=55 + soon_bonus(evs[0]["start"]),
            when=evs[0]["start"],
        ))

    # deadlines in the inbox that aren't on the calendar
    for m in emails:
        subject = m.get("subject", "")
        if not _DEADLINE_RE.search(subject):
            continue
        due = resolve_date(subject, today)
        if not due:
            continue
        on_cal = any(
            e["start"].date() == due and _words(e["title"]) & _words(subject) for e in events
        )
        if on_cal:
            continue
        title = re.sub(r"^(re|fwd?):\s*", "", subject, flags=re.I).strip()
        # The calendar entry already sits on the date, so drop "due Friday" / "by 10/3" from its name.
        short = re.sub(
            r"\s+(by|on)?\s*(today|tonight|tomorrow|" + "|".join(WEEKDAYS) + r"|(" + "|".join(MONTHS)
            + r")[a-z]*\.?\s+\d{1,2}|\d{1,2}/\d{1,2})\b.*$", "", title, flags=re.I,
        ).strip()[:60] or title[:60]
        out.append(Insight(
            kind="deadline",
            headline=f"“{title[:60]}” isn't on your calendar",
            detail=f"From {m.get('from', 'someone')}; due {_day_label(due, today)}. Easy to miss when it only lives in email.",
            score=85 + soon_bonus(datetime.combine(due, datetime.min.time(), tzinfo=tz)) + topic_bonus(subject, m.get("from", "")),
            when=datetime.combine(due, datetime.min.time(), tzinfo=tz),
            action={"type": "add_event", "event": {"title": short, "start": due.isoformat(), "all_day": True}},
        ))

    # prep for an upcoming meeting
    for m in emails:
        subject = m.get("subject", "")
        if not _PREP_RE.search(subject):
            continue
        target_day = resolve_date(subject, today)
        meetings = [e for e in timed if _MEETING_RE.search(e["title"])]
        # Best match: a meeting sharing words with the email; else the longest meeting that day.
        matches = [e for e in meetings if _words(subject) & _words(e["title"])] or sorted(
            (e for e in meetings if target_day is not None and e["start"].date() == target_day),
            key=lambda e: -(e["end"] - e["start"]).total_seconds(),
        )
        for e in matches[:1]:
            block = (e["start"] - timedelta(days=1)).replace(hour=16, minute=0)
            if block < now:
                block = max(now + timedelta(minutes=30), e["start"] - timedelta(hours=2)).replace(second=0, microsecond=0)
            out.append(Insight(
                kind="prep",
                headline=f"{m.get('from', 'Someone')}'s “{subject}” is probably for {e['title']}",
                detail=f"{e['title']} is {_day_label(e['start'].date(), today)} at {_time(e['start'])}; "
                       "worth a read beforehand.",
                score=70 + soon_bonus(e["start"]) + topic_bonus(subject, e["title"]),
                when=e["start"],
                action={"type": "add_event", "event": {
                    "title": f"Review {subject[:40]}", "start": block.strftime("%Y-%m-%dT%H:%M"), "duration_minutes": 30,
                }},
            ))
            break

    # people waiting on a reply
    for m in emails:
        subject, sender = m.get("subject", ""), m.get("from", "")
        if subject.lower().startswith("re:") and _ASK_RE.search(subject) and not _ORG_RE.search(sender):
            out.append(Insight(
                kind="reply",
                headline=f"{sender} might be waiting on you: “{re.sub(r'^re:\s*', '', subject, flags=re.I)}”",
                detail="It's a reply thread with an open question.",
                score=60 + topic_bonus(subject),
            ))

    out.sort(key=lambda x: -x.score)
    for i, ins in enumerate(out[:limit]):
        ins.id = f"{ins.kind}-{i}"
    return out[:limit]


def describe_insights(insights: list[dict[str, Any]]) -> str:
    """Compact, model-facing list for the director's note."""
    lines = []
    for ins in insights:
        fix = " (one-tap fix: add to calendar)" if (ins.get("action") or {}).get("type") == "add_event" else ""
        lines.append(f"- [{ins['kind']}] {ins['headline']}. {ins['detail']}{fix}")
    return "\n".join(lines)


def week_summary(snapshot: dict[str, Any] | None, *, tz_name: str | None = None, now: datetime | None = None) -> dict[str, Any] | None:
    """The shape of their week for the living profile: counts and day names only, no titles."""
    if not snapshot:
        return None
    tz = user_zone(tz_name)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    week = [
        s for e in snapshot.get("events", [])
        if (s := _parse(e.get("start"), tz)) and now - timedelta(hours=1) <= s <= now + timedelta(days=7)
    ]
    by_day: dict[date, int] = {}
    for s in week:
        by_day[s.date()] = by_day.get(s.date(), 0) + 1
    busiest = max(by_day.items(), key=lambda kv: kv[1], default=None)
    return {
        "events_this_week": len(week),
        "busiest_day": _day_label(busiest[0], now.date()).capitalize() if busiest else None,
        "busiest_count": busiest[1] if busiest else 0,
        "emails_scanned": len(snapshot.get("emails", [])),
        "demo": bool(snapshot.get("demo")),
    }
