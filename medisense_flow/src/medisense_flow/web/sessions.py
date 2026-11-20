"""In-memory store for report runs (one Session per submitted report).

One Session per run, always tied to the patient who started it. Every method
takes a lock, so the background threads that run CrewAI and the request
handlers can safely share it.

This is fine for ONE server process. For several workers or restarts that must
keep data, replace this class with one backed by SQLite or Redis (keep the same
method names and nothing else has to change)."""
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import settings
from .schemas import SessionStatus


@dataclass
class Session:
    id: str
    query: str
    patient_id: int
    patient_name: str
    patient_code: str
    history_text: str = ""        # summaries of earlier reports (input to the history agent)
    history_count: int = 0
    status: SessionStatus = SessionStatus.ANALYZING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    diagnostics_init: str = ""
    history_note: str = ""
    questions: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    summary: str = ""
    final_assessment: str = ""    # the updated assessment text (the chatbot reads it)
    score: int | None = None
    report_path: Path | None = None
    download_name: str = ""
    error: str | None = None


class SessionStore:
    def __init__(self) -> None:
        self._items: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(
        self,
        query: str,
        *,
        patient_id: int,
        patient_name: str,
        patient_code: str,
        history_text: str = "",
        history_count: int = 0,
    ) -> Session:
        session = Session(
            id=secrets.token_urlsafe(32),
            query=query,
            patient_id=patient_id,
            patient_name=patient_name,
            patient_code=patient_code,
            history_text=history_text,
            history_count=history_count,
        )
        with self._lock:
            self._items[session.id] = session
            return replace(session)

    def get(self, session_id: str) -> Session | None:
        """Return a snapshot copy, so callers never see half-written updates."""
        with self._lock:
            session = self._items.get(session_id)
            return replace(session) if session else None

    def update(self, session_id: str, **fields) -> None:
        with self._lock:
            session = self._items.get(session_id)
            if session is None:
                return
            for name, value in fields.items():
                setattr(session, name, value)
            session.updated_at = time.time()

    def begin_processing(self, session_id: str, answers: list[str]) -> bool:
        """Atomically move awaiting_answers -> processing and save the answers.
        Returns False if the session is in any other state (blocks double submits)."""
        with self._lock:
            session = self._items.get(session_id)
            if session is None or session.status != SessionStatus.AWAITING_ANSWERS:
                return False
            session.answers = list(answers)
            session.status = SessionStatus.PROCESSING
            session.updated_at = time.time()
            return True

    def purge_expired(self) -> None:
        """Delete sessions (and their PDF folders) older than the TTL."""
        cutoff = time.time() - settings.session_ttl_minutes * 60
        with self._lock:
            expired = [s.id for s in self._items.values() if s.updated_at < cutoff]
            for session_id in expired:
                del self._items[session_id]
        for session_id in expired:
            shutil.rmtree(settings.reports_dir / session_id, ignore_errors=True)

    def delete_for_patient(self, patient_id: int) -> None:
        """Remove every session (and PDF folder) that belongs to a patient."""
        with self._lock:
            mine = [s.id for s in self._items.values() if s.patient_id == patient_id]
            for session_id in mine:
                del self._items[session_id]
        for session_id in mine:
            shutil.rmtree(settings.reports_dir / session_id, ignore_errors=True)


store = SessionStore()


def get_store() -> SessionStore:
    return store
