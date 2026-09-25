from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR.parent / ".env")
load_dotenv(BACKEND_DIR / ".env")

# Python from python.org on macOS ships without trusted root certificates, so the
# voice websockets (Deepgram, Cartesia) fail with CERTIFICATE_VERIFY_FAILED unless
# "Install Certificates.command" was run. Point Python at certifi's bundle instead.
if not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass


def _env(name: str, default: str = "") -> str:
    """Read an env var, treating a stray inline comment as empty.

    python-dotenv reads `KEY=   # note` as the value "# note" when KEY is empty.
    """
    value = os.getenv(name, default).strip()
    return default if value.startswith("#") else value


@dataclass(frozen=True)
class Settings:
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-sonnet-5"))
    # "adaptive" (thinking on, depth set by effort) or "disabled"
    llm_thinking: str = field(default_factory=lambda: _env("LLM_THINKING", "disabled"))
    llm_effort: str = field(default_factory=lambda: _env("LLM_EFFORT", "low"))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", str(BACKEND_DIR / "data" / "sessions.db")))
    frontend_origin: str = field(default_factory=lambda: _env("FRONTEND_ORIGIN", "http://localhost:3000"))
    # ---- Gmail (Phase 3) ----
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET", ""))
    google_redirect_uri: str = field(
        default_factory=lambda: _env("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/google/callback")
    )
    # The fake sign-in popup, used automatically until Google credentials are set.
    gmail_stub: bool = field(
        default_factory=lambda: _env("GMAIL_STUB", "0" if _env("GOOGLE_CLIENT_ID") else "1") == "1"
    )
    # Offer a clearly labeled "use demo data" option for reviewers who can't sign in.
    allow_demo_data: bool = field(default_factory=lambda: _env("ALLOW_DEMO_DATA", "1") == "1")

    # ---- Voice (Phase 2) ----
    deepgram_api_key: str = field(default_factory=lambda: _env("DEEPGRAM_API_KEY", ""))
    stt_model: str = field(default_factory=lambda: _env("STT_MODEL", "nova-3-general"))
    # The voice layer: switching provider or voice is config only (tech design 8.1).
    tts_provider: str = field(default_factory=lambda: _env("TTS_PROVIDER", "cartesia"))  # cartesia | elevenlabs | openai
    tts_voice_id: str = field(default_factory=lambda: _env("TTS_VOICE_ID", ""))
    tts_model: str = field(default_factory=lambda: _env("TTS_MODEL", ""))
    # "token" streams words to TTS as they arrive (~0.3s faster); "sentence" waits for full sentences.
    # Empty = token for Cartesia (built for streamed text), sentence for the others.
    tts_text_mode: str = field(default_factory=lambda: _env("TTS_TEXT_MODE", ""))
    cartesia_api_key: str = field(default_factory=lambda: _env("CARTESIA_API_KEY", ""))
    elevenlabs_api_key: str = field(default_factory=lambda: _env("ELEVENLABS_API_KEY", ""))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY", ""))
    # Privacy: sessions (and their Google tokens, revoked first) are deleted after this many days idle.
    retention_days: float = field(default_factory=lambda: float(_env("RETENTION_DAYS", "7")))
    # End of turn: Smart Turn replies right away when it judges you're done; when it's unsure,
    # wait at most this long in silence. Pipecat's default is 3s, which on real callers meant
    # ~4.5s before every reply (the model often judged finished sentences "incomplete").
    turn_max_wait_secs: float = field(default_factory=lambda: float(_env("TURN_MAX_WAIT_SECS", "1.2")))
    # Silence on the call: first check-in after this many seconds, then offer text after the second value.
    silence_checkin_secs: float = field(default_factory=lambda: float(_env("SILENCE_CHECKIN_SECS", "5")))
    silence_offer_text_secs: float = field(default_factory=lambda: float(_env("SILENCE_OFFER_TEXT_SECS", "10")))

    def tts_key(self) -> str:
        return {
            "cartesia": self.cartesia_api_key,
            "elevenlabs": self.elevenlabs_api_key,
            "openai": self.openai_api_key,
        }.get(self.tts_provider, "")

    def voice_problem(self) -> str | None:
        """Why real voice can't run, or None if it can. The UI falls back to typed calls."""
        if not self.deepgram_api_key:
            return "DEEPGRAM_API_KEY is not set"
        if self.tts_provider not in ("cartesia", "elevenlabs", "openai"):
            return f"unknown TTS_PROVIDER {self.tts_provider!r}"
        if not self.tts_key():
            return f"{self.tts_provider.upper()}_API_KEY is not set"
        if self.tts_provider in ("cartesia", "elevenlabs") and not self.tts_voice_id:
            return "TTS_VOICE_ID is not set (run scripts/voices.py to pick one)"
        return None
