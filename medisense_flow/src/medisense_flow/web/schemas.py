"""Pydantic models for requests and responses."""
from enum import Enum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, field_validator

from .config import settings


class SessionStatus(str, Enum):
    ANALYZING = "analyzing"                # phase 1 running
    AWAITING_ANSWERS = "awaiting_answers"  # questions ready, waiting for the user
    PROCESSING = "processing"              # phase 2 running
    COMPLETE = "complete"                  # report ready
    FAILED = "failed"


def _check_name(value: str) -> str:
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("Name can't contain control characters.")
    try:
        value.encode("cp1252")  # the PDF fonts cover Latin letters (accents are fine)
    except UnicodeEncodeError:
        raise ValueError("Please write your name using English letters (accents are fine).")
    return value


PersonName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=80),
    AfterValidator(_check_name),
]
Password = Annotated[str, StringConstraints(min_length=8, max_length=128)]
ReportText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=10, max_length=settings.max_report_chars),
]
AnswerText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=settings.max_answer_chars),
]


# ---------- accounts ----------
class RegisterRequest(BaseModel):
    name: PersonName
    password: Password
    consent: bool

    @field_validator("consent")
    @classmethod
    def _must_consent(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Please agree to the saving of your records to continue.")
        return value


class LoginRequest(BaseModel):
    patient_code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    password: Annotated[str, Field(min_length=1, max_length=128)]


class DeleteAccountRequest(BaseModel):
    password: Annotated[str, Field(min_length=1, max_length=128)]


class Profile(BaseModel):
    name: str
    patient_code: str
    earlier_reports: int = 0


# ---------- reports ----------
class ReportSubmission(BaseModel):
    report: ReportText


class AnswersSubmission(BaseModel):
    # Exactly three answers, in the same order as the three questions.
    answers: Annotated[list[AnswerText], Field(min_length=3, max_length=3)]


class SessionCreated(BaseModel):
    session_id: str
    status: SessionStatus


class SessionView(BaseModel):
    session_id: str
    status: SessionStatus
    questions: list[str] | None = None   # set once status is awaiting_answers or later
    history_count: int = 0               # how many earlier reports were taken into account
    summary: str | None = None           # set once status is complete
    score: int | None = None             # None = not scored
    download_url: str | None = None      # set once status is complete
    error: str | None = None             # set when status is failed
