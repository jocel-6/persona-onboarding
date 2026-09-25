"""#6: a deeper look at the user's week, in the background.

Code (app/insights.py) catches the mechanical problems: overlaps, missing deadlines.
This pass asks a stronger model for the connections rules can't see ("the field trip
Friday lands on your planning day; want to ask Sam to cover pickup?"), strictly
grounded in the items it's given. It runs once per connection, off the conversation's
critical path, and returns nothing rather than something made up.
"""

from __future__ import annotations

import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger("persona.deep")


class DeepFinding(BaseModel):
    headline: str = Field(description="One line, under 90 characters, specific to their items.")
    detail: str = Field(description="One or two sentences: why it matters, naming the exact items involved.")
    fix: Literal["add_event", "none"] = Field(description="add_event only if a calendar block genuinely solves it.")
    fix_title: str | None = Field(default=None, description="Short event title for the fix.")
    fix_start: str | None = Field(default=None, description="Local start 'YYYY-MM-DDTHH:MM', or 'YYYY-MM-DD' all day.")
    fix_minutes: int | None = Field(default=None, description="Length in minutes for timed fixes.")


class DeepResult(BaseModel):
    findings: list[DeepFinding] = Field(description="Up to 3, best first. Empty if nothing is solid.")


SYSTEM = """You are the analyst behind Persona, a personal AI assistant. You look at one person's upcoming calendar and recent email subject lines and find what a brilliant, caring chief of staff would notice that they probably haven't: conflicts between their life and work, things that need to happen before other things, people or commitments about to fall through the cracks, a better way to use a free stretch.

Rules:
- Only use the items listed. Every finding must name the specific items it's built on. Never invent events, emails, people, or facts.
- Skip anything already covered by "Already found by rules". Skip anything medical, financial, or private.
- Be useful, not preachy: no generic advice ("get more sleep"). Each finding should make them think "oh, good catch".
- Up to 3 findings, best first. If nothing is genuinely solid, return an empty list.
- A fix is a calendar block you could propose (a reminder, prep time, a buffer). Use fix "none" when a block wouldn't help."""


async def find_deep_insights(
    client: anthropic.AsyncAnthropic,
    model: str,
    *,
    snapshot_text: str,
    rule_findings: str,
    help_topic: str | None,
    user_name: str | None,
    now_text: str,
) -> list[dict]:
    prompt = (
        f"Now: {now_text}\n"
        f"Their name: {user_name or 'unknown'}\n"
        f"What they said they need help with: {help_topic or 'not said yet'}\n\n"
        f"Their calendar and inbox:\n{snapshot_text}\n\n"
        f"Already found by rules (don't repeat):\n{rule_findings or '(none)'}"
    )
    try:
        resp = await client.messages.parse(
            model=model,
            max_tokens=4000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low"},
            output_format=DeepResult,
        )
    except (anthropic.APIError, ValueError):
        log.exception("deep insights failed; continuing with rule-based findings only")
        return []
    out = []
    for i, f in enumerate(resp.parsed_output.findings[:3] if resp.parsed_output else []):
        action = None
        if f.fix == "add_event" and f.fix_title and f.fix_start:
            event = {"title": f.fix_title[:60], "start": f.fix_start}
            if len(f.fix_start) == 10:
                event["all_day"] = True
            elif f.fix_minutes:
                event["duration_minutes"] = f.fix_minutes
            action = {"type": "add_event", "event": event}
        out.append({
            "id": f"deep-{i}", "kind": "deep", "headline": f.headline[:120], "detail": f.detail[:300],
            "score": 100 - i, "when": None, "action": action,
        })
    log.info("deep insights: %d finding(s), usage in=%s out=%s", len(out),
             getattr(resp.usage, "input_tokens", "?"), getattr(resp.usage, "output_tokens", "?"))
    return out
