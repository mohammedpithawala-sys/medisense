"""Settings for the web app. Every value can be overridden with an environment
variable (or a line in your .env file)."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Where generated PDF reports are stored (one sub-folder per session).
    reports_dir: Path = Path(os.getenv("MEDISENSE_REPORTS_DIR", "var/reports")).resolve()
    # The SQLite database file (patients and their saved reports).
    db_path: Path = Path(os.getenv("MEDISENSE_DB_PATH", "var/medisense.db")).resolve()
    # Limits for what users can send.
    max_report_chars: int = int(os.getenv("MEDISENSE_MAX_REPORT_CHARS", "8000"))
    max_answer_chars: int = int(os.getenv("MEDISENSE_MAX_ANSWER_CHARS", "1000"))
    # In-progress sessions (and their PDF files) are deleted after this many minutes.
    # Saved reports in the database are NOT affected.
    session_ttl_minutes: int = int(os.getenv("MEDISENSE_SESSION_TTL_MINUTES", "60"))
    # How many CrewAI runs may execute at the same time (protects API limits/cost).
    max_concurrent_runs: int = int(os.getenv("MEDISENSE_MAX_CONCURRENT_RUNS", "2"))
    # How many earlier reports are used as history for a returning patient.
    history_limit: int = int(os.getenv("MEDISENSE_HISTORY_LIMIT", "5"))
    # Login.
    cookie_name: str = "medisense_login"
    login_ttl_minutes: int = int(os.getenv("MEDISENSE_LOGIN_TTL_MINUTES", "120"))
    login_max_failures: int = int(os.getenv("MEDISENSE_LOGIN_MAX_FAILURES", "5"))
    login_lock_minutes: int = int(os.getenv("MEDISENSE_LOGIN_LOCK_MINUTES", "15"))
    # Set to true when the site is served over HTTPS (so the cookie is HTTPS-only).
    cookie_secure: bool = _flag("MEDISENSE_COOKIE_SECURE", False)
    # ---- report chatbot (LangChain, outside the CrewAI flows) ----
    # Model used for chat. Defaults to the model your CrewAI flows already use.
    chat_model: str = os.getenv("MEDISENSE_CHAT_MODEL") or os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    chat_base_url: str = os.getenv("MEDISENSE_CHAT_BASE_URL", "https://openrouter.ai/api/v1")
    chat_max_chars: int = int(os.getenv("MEDISENSE_CHAT_MAX_CHARS", "1000"))        # one question
    # Longest answer. "Thinking" models spend part of this on hidden reasoning, so keep it generous;
    # the prompt already asks for short answers.
    chat_max_tokens: int = int(os.getenv("MEDISENSE_CHAT_MAX_TOKENS", "1500"))
    # Optional: how hard a thinking model should think ("minimal", "low", "medium", "high").
    # Empty = leave it to the model. (A retry after an empty answer always uses "low".)
    chat_reasoning_effort: str = os.getenv("MEDISENSE_CHAT_REASONING_EFFORT", "").strip().lower()
    chat_timeout: int = int(os.getenv("MEDISENSE_CHAT_TIMEOUT", "45"))              # seconds without a reply
    chat_history_turns: int = int(os.getenv("MEDISENSE_CHAT_HISTORY_TURNS", "10"))  # exchanges sent to the model
    chat_max_questions: int = int(os.getenv("MEDISENSE_CHAT_MAX_QUESTIONS", "40"))  # per report
    chat_rate_limit: int = int(os.getenv("MEDISENSE_CHAT_RATE_LIMIT", "15"))        # questions per window per patient
    chat_rate_window: int = int(os.getenv("MEDISENSE_CHAT_RATE_WINDOW", "300"))     # seconds
    chat_max_concurrent: int = int(os.getenv("MEDISENSE_CHAT_MAX_CONCURRENT", "8")) # answers streaming at once


settings = Settings()
