"""The pages. `/` is the app (signed-in patients only) and `/login` is the sign-in page."""
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..auth import OptionalPatient
from ..config import TEMPLATES_DIR, settings

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/")
def index(request: Request, patient: OptionalPatient) -> Response:
    if patient is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "max_report_chars": settings.max_report_chars,
            "max_answer_chars": settings.max_answer_chars,
        },
    )


@router.get("/login")
def login_page(request: Request, patient: OptionalPatient) -> Response:
    if patient is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})
