"""Turns a patient's saved reports into the short text the history agent reads."""
from datetime import datetime, timezone

from . import db
from .config import settings


def load_history(patient_id: int, exclude_session_id: str | None = None) -> tuple[str, int]:
    """Return (history_text, number_of_reports). Only summaries, scores and dates are
    used (not full reports), which keeps prompts short and limits what is sent to the AI.
    The name and Patient ID are never included."""
    rows = db.get_recent_reports(patient_id, settings.history_limit, exclude_session_id)
    if not rows:
        return "", 0
    now = datetime.now(timezone.utc)
    lines = []
    for number, row in enumerate(rows, start=1):
        when = datetime.fromisoformat(row["created_at"])
        days = (now - when).days
        age = "today" if days == 0 else ("1 day ago" if days == 1 else f"{days} days ago")
        score = f", self-check score {row['score']}/10" if row["score"] is not None else ""
        summary = " ".join(row["summary"].split())  # one line per report
        lines.append(f"Report {number} ({when:%Y-%m-%d}, {age}{score}): {summary}")
    return "\n".join(lines), len(rows)
