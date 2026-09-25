"""#8: draft a reply to someone who's waiting on the user. Saved as a Gmail draft only on their tap; never sent.

Persona only sees the subject line (gmail.metadata can't read bodies), so the draft stays
general and honest: acknowledge, answer with the free time the calendar shows, ask the
open question back. The user edits it before saving.
"""

from __future__ import annotations

import logging

import anthropic

from .brain import no_em_dashes

log = logging.getLogger("persona.drafts")

SYSTEM = """You write short, warm email replies for someone, in their voice: casual, kind, specific, never salesy. You only know the subject line of the email you're replying to (not its body), so don't invent details about what they said. If a free time is given, offer it concretely. 2-4 short sentences. No subject line, no greeting fluff beyond "Hey <name>," and no signature beyond their first name if given. Never use em dashes."""


async def write_draft(
    client: anthropic.AsyncAnthropic,
    model: str,
    *,
    to_name: str,
    subject: str,
    user_name: str | None,
    suggested_time: str | None,
) -> str:
    prompt = (
        f"Reply to {to_name}. Their email's subject: \"{subject}\".\n"
        f"Free time on the user's calendar: {suggested_time or 'unknown'}.\n"
        f"The user's first name: {user_name or 'unknown (no signature)'}."
    )
    extra = {} if model.startswith("claude-haiku") else {"thinking": {"type": "disabled"}}  # a 3-line reply needs no thinking
    resp = await client.messages.create(
        model=model, max_tokens=400, system=SYSTEM, messages=[{"role": "user", "content": prompt}], **extra,
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    return no_em_dashes(text)
