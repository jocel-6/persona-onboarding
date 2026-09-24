"""SQLite session store keyed by session ID.

State is stored as one JSON document per session, so a server restart or a page
refresh resumes exactly where the conversation stopped. Google tokens will live in
a separate table (Phase 3) and never inside the state document.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .state import OnboardingState


class SessionStore:
    def __init__(self, path: str | Path):
        self._path = str(path)
        self._lock = threading.Lock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                   id TEXT PRIMARY KEY,
                   state TEXT NOT NULL,
                   updated_at REAL NOT NULL
               )"""
        )
        self._conn.commit()

    def get(self, session_id: str) -> OnboardingState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return OnboardingState.model_validate_json(row[0]) if row else None

    def save(self, state: OnboardingState) -> None:
        state.updated_at = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, state, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
                (state.session_id, state.model_dump_json(), state.updated_at),
            )
            self._conn.commit()

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._conn.commit()
