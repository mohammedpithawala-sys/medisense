"""The bridge between FastAPI and CrewAI.

The functions here are plain `def` (not async) on purpose: FastAPI runs them in
its thread pool, so the long, blocking CrewAI calls never freeze the server.
CrewAI is only touched here; the PDF and the database are handled right after
the AI has finished.

Privacy: the patient's name and Patient ID are never sent to the AI provider."""
import logging
import re
import threading
from datetime import datetime

from medisense_flow.main import AnalysisFlow, ReportFlow

from . import db
from .config import settings
from .pdf_report import ReportData, build_pdf
from .schemas import SessionStatus
from .sessions import store

logger = logging.getLogger("medisense.runner")

# Limits how many CrewAI runs happen at once; the others wait their turn.
_run_slots = threading.BoundedSemaphore(settings.max_concurrent_runs)

QUESTION_ATTEMPTS = 2  # the model sometimes returns no structured questions
USER_FACING_ERROR = (
    "Something went wrong while processing your request. Please try again in a moment."
)


class IncompleteResultError(Exception):
    """CrewAI finished but did not give us everything we need."""


def _safe_filename_part(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:40] or "patient"


def run_analysis(session_id: str) -> None:
    """Phase 1: analyze the report (and the patient's history) and produce three follow-up questions."""
    session = store.get(session_id)
    if session is None:
        return
    try:
        with _run_slots:
            for attempt in range(1, QUESTION_ATTEMPTS + 1):
                flow = AnalysisFlow()
                flow.kickoff(inputs={"query": session.query, "history": session.history_text})
                state = flow.state
                questions = [state.q1.strip(), state.q2.strip(), state.q3.strip()]
                if all(questions) and state.diagnostics_init.strip():
                    break
                logger.warning("Session %s: empty questions (attempt %s)", session_id[:6], attempt)
            else:
                raise IncompleteResultError("No follow-up questions were generated.")
        store.update(
            session_id,
            status=SessionStatus.AWAITING_ANSWERS,
            diagnostics_init=state.diagnostics_init,
            history_note=state.history_note,
            questions=questions,
        )
    except Exception:
        # logger.exception records the traceback, but never the user's text.
        logger.exception("Session %s: analysis failed", session_id[:6])
        store.update(session_id, status=SessionStatus.FAILED, error=USER_FACING_ERROR)


def run_report(session_id: str) -> None:
    """Phase 2: re-diagnose with the answers, score, summarize, build the PDF, save to the database."""
    session = store.get(session_id)
    if session is None:
        return
    try:
        with _run_slots:
            flow = ReportFlow()
            flow.kickoff(
                inputs={
                    "query": session.query,
                    "diagnostics_init": session.diagnostics_init,
                    "history_note": session.history_note,
                    "q1": session.questions[0],
                    "q2": session.questions[1],
                    "q3": session.questions[2],
                    "a1": session.answers[0],
                    "a2": session.answers[1],
                    "a3": session.answers[2],
                }
            )
        state = flow.state
        if not state.rediagnosis.strip():
            raise IncompleteResultError("No updated assessment was generated.")

        score = state.score or None  # 0 means "scoring failed" -> shown as not scored
        qa = list(zip(session.questions, session.answers))
        created = datetime.now()
        pdf_path = settings.reports_dir / session_id / "diagnosis_report.pdf"
        build_pdf(
            pdf_path,
            ReportData(
                patient_name=session.patient_name,
                patient_code=session.patient_code,
                report_id=session_id[:8].upper(),
                created=created,
                submitted_report=session.query,
                initial_assessment=session.diagnostics_init,
                qa=qa,
                final_assessment=state.rediagnosis,
                summary=state.summary,
                score=score,
                history_note=session.history_note,
            ),
        )
    except Exception:
        logger.exception("Session %s: report generation failed", session_id[:6])
        store.update(session_id, status=SessionStatus.FAILED, error=USER_FACING_ERROR)
        return

    # The patient already has their PDF, so a database problem must not hide it.
    try:
        db.save_report(
            session_id=session_id,
            patient_id=session.patient_id,
            submitted_report=session.query,
            initial_assessment=session.diagnostics_init,
            qa=qa,
            final_assessment=state.rediagnosis,
            summary=state.summary,
            score=score,
        )
    except Exception:
        logger.exception("Session %s: could not save the report to the database", session_id[:6])

    store.update(
        session_id,
        status=SessionStatus.COMPLETE,
        summary=state.summary,
        final_assessment=state.rediagnosis,
        score=score,
        report_path=pdf_path,
        download_name=f"MediSense_Report_{_safe_filename_part(session.patient_name)}_{created:%Y-%m-%d}.pdf",
    )
