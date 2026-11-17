"""Accounts and login for the web app (Patient ID + password).

- Passwords are stored only as Argon2 hashes.
- A successful login gives the browser a random token in an HttpOnly cookie.
- Wrong passwords are counted per Patient ID and lock further attempts for a while.
- All of it is plugged into FastAPI through dependencies (see `CurrentPatient`).
"""
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Response
from fastapi.security import APIKeyCookie

from . import db
from .config import settings

# ---------- passwords ----------
_hasher = PasswordHasher()
# Checked when the Patient ID doesn't exist, so "unknown ID" and "wrong password"
# take the same time and can't be told apart.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def burn_time(password: str) -> None:
    verify_password(_DUMMY_HASH, password)


def normalize_code(raw: str) -> str:
    """'ms-4f7k-92qd ' -> 'MS-4F7K-92QD'"""
    return raw.strip().upper()


# ---------- limiting wrong passwords ----------
class LoginThrottle:
    """Counts wrong passwords per Patient ID (known or not, so behaviour is identical).
    After `max_failures` wrong tries in a row the ID is locked for `lock_seconds`."""

    def __init__(self, max_failures: int, lock_seconds: int) -> None:
        self._max = max_failures
        self._lock_seconds = lock_seconds
        self._entries: dict[str, list[float]] = {}  # key -> [failures, last_failure, locked_until]
        self._guard = threading.Lock()

    def seconds_locked(self, key: str) -> int:
        now = time.time()
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                return 0
            if entry[2] > now:
                return int(entry[2] - now) + 1
            if now - entry[1] > self._lock_seconds:  # old failures are forgiven
                del self._entries[key]
            return 0

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._guard:
            if len(self._entries) > 10_000:  # keep memory bounded
                self._entries = {k: v for k, v in self._entries.items() if now - v[1] <= self._lock_seconds}
            entry = self._entries.setdefault(key, [0, now, 0.0])
            if now - entry[1] > self._lock_seconds:
                entry[0] = 0
            entry[0] += 1
            entry[1] = now
            if entry[0] >= self._max:
                entry[2] = now + self._lock_seconds
                entry[0] = 0

    def record_success(self, key: str) -> None:
        with self._guard:
            self._entries.pop(key, None)


throttle = LoginThrottle(settings.login_max_failures, settings.login_lock_minutes * 60)


def get_throttle() -> LoginThrottle:
    return throttle


# ---------- logged-in browsers ----------
class LoginStore:
    """token -> (patient_id, expires_at). In memory, like the report sessions."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._tokens: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def create(self, patient_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = time.time()
            self._tokens = {t: v for t, v in self._tokens.items() if v[1] > now}  # drop expired
            self._tokens[token] = (patient_id, now + self._ttl)
        return token

    def get(self, token: str) -> int | None:
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return None
            if entry[1] <= time.time():
                del self._tokens[token]
                return None
            return entry[0]

    def delete(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def delete_for_patient(self, patient_id: int) -> None:
        with self._lock:
            self._tokens = {t: v for t, v in self._tokens.items() if v[0] != patient_id}


logins = LoginStore(settings.login_ttl_minutes * 60)


def get_logins() -> LoginStore:
    return logins


def set_login_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.login_ttl_minutes * 60,
        httponly=True,                    # JavaScript can't read it
        samesite="lax",                   # not sent on cross-site POSTs
        secure=settings.cookie_secure,    # HTTPS-only when you deploy with HTTPS
        path="/",
    )


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(settings.cookie_name, path="/")


# ---------- the FastAPI dependency ----------
@dataclass(frozen=True)
class Patient:
    id: int
    name: str
    patient_code: str


login_cookie = APIKeyCookie(name=settings.cookie_name, auto_error=False)


def optional_patient(
    token: Annotated[str | None, Depends(login_cookie)],
    store: Annotated[LoginStore, Depends(get_logins)],
) -> Patient | None:
    """The signed-in patient, or None. Used by the pages, which redirect instead of failing."""
    patient_id = store.get(token) if token else None
    row = db.get_patient(patient_id) if patient_id is not None else None
    if row is None:
        return None
    return Patient(id=row["id"], name=row["name"], patient_code=row["patient_code"])


OptionalPatient = Annotated[Patient | None, Depends(optional_patient)]


def current_patient(patient: OptionalPatient) -> Patient:
    """Use as `patient: CurrentPatient`. Answers 401 unless the browser is logged in."""
    if patient is None:
        raise HTTPException(status_code=401, detail="Please sign in to continue.")
    return patient


CurrentPatient = Annotated[Patient, Depends(current_patient)]
