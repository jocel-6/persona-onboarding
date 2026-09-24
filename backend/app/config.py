from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR.parent / ".env")
load_dotenv(BACKEND_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-sonnet-5"))
    # "adaptive" (thinking on, depth set by effort) or "disabled"
    llm_thinking: str = field(default_factory=lambda: os.getenv("LLM_THINKING", "disabled"))
    llm_effort: str = field(default_factory=lambda: os.getenv("LLM_EFFORT", "low"))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", str(BACKEND_DIR / "data" / "sessions.db")))
    frontend_origin: str = field(default_factory=lambda: os.getenv("FRONTEND_ORIGIN", "http://localhost:3000"))
    # Phase 1 stand-in for the real Google sign-in (Phase 3). Off in production.
    gmail_stub: bool = field(default_factory=lambda: os.getenv("GMAIL_STUB", "1") == "1")
