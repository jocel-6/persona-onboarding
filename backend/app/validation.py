"""Deterministic checks on every value the model tries to save.

The model is good at understanding messy input but can still save a whole
sentence as a name. These checks catch that; the rejection reason goes back to
the model as a tool_result so it can recover naturally.
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# Starts with a letter in any script; then letters, digits (joke names like
# "R2D2" are welcome), and the punctuation real names use.
_NAME_CHARS_RE = re.compile(r"^[^\W\d_][\w'’.\- ]*$", re.UNICODE)
# Words that show the model captured a phrase, not a name.
_SENTENCE_WORDS = {
    "i", "i'm", "im", "my", "is", "am", "the", "and", "call", "name", "you", "want",
    "please", "just", "it's", "its", "me", "hi", "hello", "hey",
}


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().strip("\"'“”‘’").strip()


def check_person_name(value: str, *, max_words: int = 4, max_len: int = 40) -> tuple[str | None, str | None]:
    """Return (clean_value, None) if acceptable, else (None, reason)."""
    v = clean(value)
    if not v:
        return None, "empty"
    if len(v) > max_len:
        return None, f"too long ({len(v)} chars); save just the name"
    words = v.split(" ")
    if len(words) > max_words:
        return None, "looks like a sentence, not a name; save just the name"
    if any(w.lower() in _SENTENCE_WORDS for w in words):
        return None, "contains filler words (like 'my', 'is', 'call'); save just the name"
    if not _NAME_CHARS_RE.match(v):
        return None, "contains symbols; save just the name"
    return v, None


def check_agent_name(value: str) -> tuple[str | None, str | None]:
    return check_person_name(value, max_words=3, max_len=30)


def check_help_topic(value: str) -> tuple[str | None, str | None]:
    v = clean(value)
    if len(v) < 3:
        return None, "too short to be useful; ask what they need help with"
    if len(v) > 240:
        return None, "too long; save a short summary in the user's own words"
    return v, None


def check_email(value: str) -> tuple[str | None, str | None]:
    v = clean(value).lower()
    if not _EMAIL_RE.match(v):
        return None, "not a valid email address"
    return v, None


# Mood cues code can catch before the model replies. Only phrases aimed at the
# conversation itself: "ugh, school emails" is venting about life, not about us.
_RUSHED_RE = re.compile(
    r"\b(hurry|be quick|make it quick|keep it quick|quickly|make it fast|in a rush|rushing|no time|don'?t have (much )?time|short on time|"
    r"running late|gotta go|got to go|have to go|meeting in|call in|leaving in|"
    r"in (a|\d+|five|two|ten) (sec|second|min|minute)s?)\b",
    re.IGNORECASE,
)
_FRUSTRATED_RE = re.compile(
    r"(just get on with it|get to the point|this is (so )?(annoying|pointless|stupid|taking forever)|"
    r"stop asking|already (told|said)|how many times|waste of (my )?time|are you (serious|kidding)|"
    r"you'?re not listening|seriously\?|omg stop)",
    re.IGNORECASE,
)


def mood_cue(text: str) -> str | None:
    """'frustrated' or 'rushed' when the words make it obvious, else None (the model decides)."""
    if _FRUSTRATED_RE.search(text):
        return "frustrated"
    if _RUSHED_RE.search(text):
        return "rushed"
    return None


def is_short_answer(text: str) -> bool:
    """Three words or fewer: a hint the user may be rushed, not proof."""
    return len(re.findall(r"\w+", text)) <= 3
