"""Accounts.

POST /api/auth/register        create a patient record -> returns the new Patient ID, signs in
POST /api/auth/login           Patient ID + password   -> signs in
POST /api/auth/logout          signs out
GET  /api/auth/me              who is signed in (used when the page loads)
POST /api/auth/delete-account  permanently delete the record and all saved reports
"""
import logging
import math
import shutil
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .. import db
from ..auth import (
    CurrentPatient,
    LoginStore,
    LoginThrottle,
    burn_time,
    clear_login_cookie,
    get_logins,
    get_throttle,
    hash_password,
    normalize_code,
    set_login_cookie,
    verify_password,
)
from ..config import settings
from ..schemas import DeleteAccountRequest, LoginRequest, Profile, RegisterRequest
from ..sessions import SessionStore, get_store

logger = logging.getLogger("medisense.auth")
router = APIRouter(prefix="/api/auth", tags=["accounts"])

LoginsDep = Annotated[LoginStore, Depends(get_logins)]
ThrottleDep = Annotated[LoginThrottle, Depends(get_throttle)]
StoreDep = Annotated[SessionStore, Depends(get_store)]


def _profile(patient: dict) -> Profile:
    return Profile(
        name=patient["name"],
        patient_code=patient["patient_code"],
        earlier_reports=db.count_reports(patient["id"]),
    )


def _check_not_locked(throttle: LoginThrottle, code: str) -> None:
    wait = throttle.seconds_locked(code)
    if wait:
        minutes = max(1, math.ceil(wait / 60))
        raise HTTPException(
            status_code=429,
            detail=f"Too many wrong passwords. Try again in about {minutes} minute(s).",
            headers={"Retry-After": str(wait)},
        )


@router.post("/register", status_code=201)
def register(body: RegisterRequest, response: Response, logins: LoginsDep) -> Profile:
    patient = db.create_patient(body.name, hash_password(body.password))
    set_login_cookie(response, logins.create(patient["id"]))
    return Profile(name=patient["name"], patient_code=patient["patient_code"], earlier_reports=0)


@router.post("/login")
def login(body: LoginRequest, response: Response, logins: LoginsDep, throttle: ThrottleDep) -> Profile:
    code = normalize_code(body.patient_code)
    _check_not_locked(throttle, code)

    patient = db.get_patient_by_code(code)
    if patient is None:
        burn_time(body.password)  # same delay as a real check
        ok = False
    else:
        ok = verify_password(patient["password_hash"], body.password)

    if not ok:
        throttle.record_failure(code)
        # One message for "no such ID" and "wrong password".
        raise HTTPException(status_code=401, detail="Patient ID or password is incorrect.")

    throttle.record_success(code)
    db.touch_login(patient["id"])
    set_login_cookie(response, logins.create(patient["id"]))
    return _profile(patient)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, logins: LoginsDep) -> None:
    token = request.cookies.get(settings.cookie_name)
    if token:
        logins.delete(token)
    clear_login_cookie(response)


@router.get("/me")
def me(patient: CurrentPatient) -> Profile:
    row = db.get_patient(patient.id)
    return _profile(row)


@router.post("/delete-account", status_code=204)
def delete_account(
    body: DeleteAccountRequest,
    response: Response,
    patient: CurrentPatient,
    logins: LoginsDep,
    throttle: ThrottleDep,
    store: StoreDep,
) -> None:
    _check_not_locked(throttle, patient.patient_code)
    row = db.get_patient(patient.id)
    if row is None or not verify_password(row["password_hash"], body.password):
        throttle.record_failure(patient.patient_code)
        # 403, not 401: the browser is signed in, the password confirmation was wrong.
        raise HTTPException(status_code=403, detail="Password is incorrect.")
    throttle.record_success(patient.patient_code)

    store.delete_for_patient(patient.id)   # in-progress sessions and their PDF files
    logins.delete_for_patient(patient.id)  # every signed-in browser of this patient
    db.delete_patient(patient.id)          # the record and (cascade) all saved reports
    clear_login_cookie(response)
    logger.info("A patient record was deleted.")
