"""Process-wide singletons shared by the HTTP API and the voice pipeline."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from .brain import Brain
from .config import Settings
from .store import SessionStore

settings = Settings()
store = SessionStore(settings.db_path)
brain = Brain(settings)

# One lock per session: text turns, voice turns, and events never overlap.
locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
