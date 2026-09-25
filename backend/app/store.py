"""SQLite session store keyed by session ID.

State is stored as one JSON document per session, so a server restart or a page
refresh resumes exactly where the conversation stopped. Google tokens live in
a separate table and never inside the state document, so they can't leak to the
browser or the model through state.
"""

from __future__ import annotations

import json
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
        # Per-turn metrics for the dashboard (#16): latency, tokens, cost. No content.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS turn_metrics (
                   ts REAL NOT NULL,
                   session_id TEXT NOT NULL,
                   channel TEXT NOT NULL,
                   model TEXT NOT NULL,
                   ttft_ms REAL,
                   total_ms REAL,
                   first_audio_ms REAL,
                   first_audio_via TEXT,
                   input_tokens INTEGER,
                   output_tokens INTEGER,
                   cache_read_tokens INTEGER,
                   cache_write_tokens INTEGER,
                   cost_usd REAL
               )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS google_tokens (
                   session_id TEXT PRIMARY KEY,
                   tokens TEXT NOT NULL,
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
            self._conn.execute("DELETE FROM google_tokens WHERE session_id = ?", (session_id,))
            self._conn.commit()

    # ---- metrics ----

    def record_turn(self, **m) -> None:
        cols = ("ts", "session_id", "channel", "model", "ttft_ms", "total_ms", "first_audio_ms", "first_audio_via",
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")
        m.setdefault("ts", time.time())
        with self._lock:
            self._conn.execute(
                f"INSERT INTO turn_metrics ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                tuple(m.get(c) for c in cols),
            )
            self._conn.commit()

    def record_first_audio(self, session_id: str, ms: float, via: str) -> None:
        """Voice: attach first-audio latency to that session's latest turn."""
        with self._lock:
            self._conn.execute(
                "UPDATE turn_metrics SET first_audio_ms = ?, first_audio_via = ? WHERE rowid = "
                "(SELECT rowid FROM turn_metrics WHERE session_id = ? AND channel = 'voice' ORDER BY ts DESC LIMIT 1)",
                (ms, via, session_id),
            )
            self._conn.commit()

    def turn_rows(self, since: float) -> list[dict]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM turn_metrics WHERE ts >= ?", (since,))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def session_states(self, since: float) -> list[OnboardingState]:
        with self._lock:
            rows = self._conn.execute("SELECT state FROM sessions WHERE updated_at >= ?", (since,)).fetchall()
        return [OnboardingState.model_validate_json(r[0]) for r in rows]

    def stale_session_ids(self, older_than_secs: float) -> list[str]:
        cutoff = time.time() - older_than_secs
        with self._lock:
            rows = self._conn.execute("SELECT id FROM sessions WHERE updated_at < ?", (cutoff,)).fetchall()
        return [r[0] for r in rows]

    # ---- Google tokens (server-side only) ----

    def save_tokens(self, session_id: str, tokens: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO google_tokens (session_id, tokens, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET tokens = excluded.tokens, updated_at = excluded.updated_at",
                (session_id, json.dumps(tokens), time.time()),
            )
            self._conn.commit()

    def get_tokens(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT tokens FROM google_tokens WHERE session_id = ?", (session_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def delete_tokens(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM google_tokens WHERE session_id = ?", (session_id,))
            self._conn.commit()
