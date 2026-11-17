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


settings = Settings()
