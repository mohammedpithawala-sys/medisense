"""FastAPI entry point.

Run (from the project root):   uv run fastapi dev
The entrypoint is set in pyproject.toml:  [tool.fastapi] entrypoint = "medisense_flow.web.app:app"
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import STATIC_DIR, settings
from .routers import api, auth, chat, documents, pages

settings.reports_dir.mkdir(parents=True, exist_ok=True)
db.init_db()  # creates var/medisense.db and its tables the first time

app = FastAPI(title="MediSense", docs_url="/docs")

_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")
_UPLOAD_SLACK = 64 * 1024  # multipart boundaries and form fields add a little to the file's size


# Registered before `security_headers`, which therefore wraps it: this rejection still gets those headers.
@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    """Turn away an oversized upload by its declared size, before the server reads or stores the body."""
    if request.url.path == documents.EXTRACT_PATH and request.method == "POST":
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > settings.max_upload_bytes + _UPLOAD_SLACK:
            return JSONResponse(
                {"detail": f"This file is larger than {settings.max_upload_mb} MB."}, status_code=413
            )
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/") or request.url.path in ("/", "/login"):
        response.headers.setdefault("Cache-Control", "no-store")  # never cache medical data or login state
    if request.url.path not in _DOC_PATHS:  # the /docs page loads scripts from a CDN
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        )
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(pages.router)
app.include_router(auth.router)
app.include_router(api.router)
app.include_router(chat.router)
app.include_router(documents.router)
