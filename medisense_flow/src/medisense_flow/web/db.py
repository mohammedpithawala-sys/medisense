"""SQLite storage for patients and their saved reports.

Uses Python's built-in sqlite3. Every function opens its own short-lived
connection, so it is safe to call from many threads at once (WAL mode lets
readers and a writer work side by side). All queries use ? placeholders, so
user text can never change the SQL."""
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .config import settings

# No 0/O/1/I so a Patient ID is easy to read and write down.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_code  TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         TEXT NOT NULL UNIQUE,
    patient_id         INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    submitted_report   TEXT NOT NULL,
    initial_assessment TEXT NOT NULL,
    qa_json            TEXT NOT NULL,
    final_assessment   TEXT NOT NULL,
    summary            TEXT NOT NULL,
    score              INTEGER,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_patient ON reports(patient_id, created_at DESC);
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'normal',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(patient_id, session_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create the database file and tables if they don't exist yet."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)


# ---------- patients ----------
def _new_code() -> str:
    pick = lambda n: "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n))
    return f"MS-{pick(4)}-{pick(4)}"


def create_patient(name: str, password_hash: str) -> dict:
    """Insert a patient and return {id, patient_code, name}."""
    for _ in range(10):  # a code clash is astronomically unlikely, but handle it anyway
        code = _new_code()
        try:
            with connect() as conn:
                cur = conn.execute(
                    "INSERT INTO patients (patient_code, name, password_hash, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (code, name, password_hash, _now()),
                )
                return {"id": cur.lastrowid, "patient_code": code, "name": name}
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Could not create a unique Patient ID.")


def get_patient_by_code(patient_code: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM patients WHERE patient_code = ?", (patient_code,)).fetchone()
    return dict(row) if row else None


def get_patient(patient_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    return dict(row) if row else None


def touch_login(patient_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE patients SET last_login_at = ? WHERE id = ?", (_now(), patient_id))


def delete_patient(patient_id: int) -> None:
    """Delete a patient and (through ON DELETE CASCADE) all their reports."""
    with connect() as conn:
        conn.execute("DELETE FROM patients WHERE id = ?", (patient_id,))


# ---------- reports ----------
def save_report(
    *,
    session_id: str,
    patient_id: int,
    submitted_report: str,
    initial_assessment: str,
    qa: list[tuple[str, str]],
    final_assessment: str,
    summary: str,
    score: int | None,
) -> None:
    """Save a finished report. Saving the same session twice does nothing."""
    qa_json = json.dumps([{"question": q, "answer": a} for q, a in qa], ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            "INSERT INTO reports (session_id, patient_id, submitted_report, initial_assessment, "
            "qa_json, final_assessment, summary, score, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO NOTHING",
            (session_id, patient_id, submitted_report, initial_assessment, qa_json,
             final_assessment, summary, score, _now()),
        )


def get_recent_reports(patient_id: int, limit: int, exclude_session_id: str | None = None) -> list[dict]:
    """The patient's newest reports first (only fields needed as history)."""
    sql = "SELECT summary, score, created_at FROM reports WHERE patient_id = ?"
    params: list = [patient_id]
    if exclude_session_id:
        sql += " AND session_id != ?"
        params.append(exclude_session_id)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_report_by_session(patient_id: int, session_id: str) -> dict | None:
    """One finished report of this patient (None if it isn't theirs or doesn't exist)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT submitted_report, initial_assessment, qa_json, final_assessment, summary, score, created_at "
            "FROM reports WHERE patient_id = ? AND session_id = ?",
            (patient_id, session_id),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["qa"] = [(item["question"], item["answer"]) for item in json.loads(data.pop("qa_json"))]
    return data


def count_reports(patient_id: int) -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM reports WHERE patient_id = ?", (patient_id,)).fetchone()
    return int(row["n"])


# ---------- chat messages ----------
def add_chat_message(patient_id: int, session_id: str, role: str, content: str, kind: str = "normal") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages (patient_id, session_id, role, content, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (patient_id, session_id, role, content, kind, _now()),
        )


def get_chat_messages(patient_id: int, session_id: str, limit: int | None = None) -> list[dict]:
    """Oldest first. With `limit`, only the newest `limit` messages (still oldest first)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content, kind, created_at FROM ("
            "  SELECT id, role, content, kind, created_at FROM chat_messages "
            "  WHERE patient_id = ? AND session_id = ? ORDER BY id DESC LIMIT ?"
            ") ORDER BY id ASC",
            (patient_id, session_id, -1 if limit is None else limit),
        ).fetchall()
    return [dict(r) for r in rows]


def count_chat_questions(patient_id: int, session_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM chat_messages WHERE patient_id = ? AND session_id = ? AND role = 'user'",
            (patient_id, session_id),
        ).fetchone()
    return int(row["n"])


def clear_chat(patient_id: int, session_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM chat_messages WHERE patient_id = ? AND session_id = ?", (patient_id, session_id))
