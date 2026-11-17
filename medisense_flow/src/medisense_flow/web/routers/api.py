"""The CrewAI processing API. Every endpoint needs a signed-in patient and only
ever touches that patient's own sessions.

POST /api/sessions                    1. submit the report  -> starts analysis
GET  /api/sessions/{id}                  poll status / get the questions / get the result
POST /api/sessions/{id}/answers       2. submit the answers -> starts the final report
GET  /api/sessions/{id}/report        3. download the finished PDF
"""
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path
from fastapi.responses import FileResponse

from ..auth import CurrentPatient, Patient
from ..config import settings
from ..history import load_history
from ..runner import run_analysis, run_report
from ..schemas import (
    AnswersSubmission,
    ReportSubmission,
    SessionCreated,
    SessionStatus,
    SessionView,
)
from ..sessions import Session, SessionStore, get_store

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

StoreDep = Annotated[SessionStore, Depends(get_store)]
# Session ids are 43 URL-safe characters; reject anything else before touching the store.
SessionId = Annotated[str, Path(min_length=40, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


def _view(session: Session) -> SessionView:
    done = session.status == SessionStatus.COMPLETE
    return SessionView(
        session_id=session.id,
        status=session.status,
        questions=session.questions or None,
        history_count=session.history_count,
        summary=session.summary if done else None,
        score=session.score,
        download_url=f"/api/sessions/{session.id}/report" if done else None,
        error=session.error,
    )


def _get_or_404(store: SessionStore, session_id: str, patient: Patient) -> Session:
    session = store.get(session_id)
    # Someone else's session looks exactly like one that doesn't exist.
    if session is None or session.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="Session not found or expired.")
    return session


@router.post("", status_code=202)
def create_session(
    body: ReportSubmission,
    background_tasks: BackgroundTasks,
    store: StoreDep,
    patient: CurrentPatient,
) -> SessionCreated:
    store.purge_expired()  # cheap housekeeping: drop old sessions and their files
    history_text, history_count = load_history(patient.id)
    session = store.create(
        body.report,
        patient_id=patient.id,
        patient_name=patient.name,
        patient_code=patient.patient_code,
        history_text=history_text,
        history_count=history_count,
    )
    background_tasks.add_task(run_analysis, session.id)
    return SessionCreated(session_id=session.id, status=session.status)


@router.get("/{session_id}")
def get_session(session_id: SessionId, store: StoreDep, patient: CurrentPatient) -> SessionView:
    return _view(_get_or_404(store, session_id, patient))


@router.post("/{session_id}/answers", status_code=202)
def submit_answers(
    session_id: SessionId,
    body: AnswersSubmission,
    background_tasks: BackgroundTasks,
    store: StoreDep,
    patient: CurrentPatient,
) -> SessionView:
    session = _get_or_404(store, session_id, patient)
    if not store.begin_processing(session_id, body.answers):
        raise HTTPException(
            status_code=409,
            detail=f"Answers can't be submitted while the session is '{session.status.value}'.",
        )
    background_tasks.add_task(run_report, session_id)
    return _view(_get_or_404(store, session_id, patient))


@router.get("/{session_id}/report")
def download_report(session_id: SessionId, store: StoreDep, patient: CurrentPatient) -> FileResponse:
    session = _get_or_404(store, session_id, patient)
    path = session.report_path
    if session.status != SessionStatus.COMPLETE or path is None:
        raise HTTPException(status_code=404, detail="The report isn't ready yet.")
    # Defence in depth: the file must live inside the reports folder.
    resolved = path.resolve()
    if not resolved.is_relative_to(settings.reports_dir) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Report file not found.")
    return FileResponse(
        resolved,
        media_type="application/pdf",
        filename=session.download_name or "MediSense_Report.pdf",  # sent as an attachment
        headers={"Cache-Control": "no-store"},
    )
