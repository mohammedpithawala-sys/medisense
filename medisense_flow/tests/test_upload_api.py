"""API tests: upload -> extract -> existing analysis pipeline, with a real signed-in client."""
import pytest
from fastapi.testclient import TestClient

from medisense_flow.web.app import app
from medisense_flow.web.config import settings

from . import sample_files as sf
from .conftest import CALLS

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        reg = c.post("/api/auth/register", json={"name": "Test Patient", "password": "correct-horse-1", "consent": True})
        assert reg.status_code in (200, 201), reg.text
        yield c


def upload(client, name, data, mime="application/octet-stream"):
    return client.post("/api/documents/extract", files={"file": (name, data, mime)})


def test_requires_login():
    with TestClient(app) as anonymous:
        r = upload(anonymous, "a.pdf", sf.make_pdf([["hello"]]))
        assert r.status_code == 401


def test_pdf_upload_returns_text(client):
    r = upload(client, "report.pdf", sf.make_pdf([["Page one symptoms"], ["Page two results"]]), "application/pdf")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "pdf" and body["pages"] == 2
    assert "Page one symptoms" in body["text"] and "Page two results" in body["text"]
    assert body["characters"] == len(body["text"])
    assert r.headers["cache-control"] == "no-store"          # medical text must never be cached


def test_docx_upload_returns_text(client):
    r = upload(client, "summary.docx", sf.make_docx(), DOCX)
    assert r.status_code == 200, r.text
    assert "Hemoglobin | 11.2 g/dL | 12-16" in r.json()["text"]


def test_browser_content_type_is_not_trusted(client):
    # Right bytes + wrong Content-Type is fine; wrong bytes + right Content-Type is not.
    assert upload(client, "a.pdf", sf.make_pdf([["ok text here"]]), "text/plain").status_code == 200
    assert upload(client, "a.pdf", b"MZ not a pdf", "application/pdf").status_code == 422


@pytest.mark.parametrize("name,data,status", [
    ("notes.txt", b"hello", 415),
    ("old.doc", b"x", 415),
    ("empty.pdf", b"", 422),
    ("scan.pdf", sf.make_blank_pdf(), 422),
    ("empty.docx", sf.make_empty_docx(), 422),
    ("broken.pdf", sf.make_pdf([["hello"]])[:60], 422),
    ("locked.pdf", sf.encrypt_pdf(sf.make_pdf([["x"]]), "pw123456"), 422),
])
def test_bad_uploads_get_clear_json_errors(client, name, data, status):
    r = upload(client, name, data)
    assert r.status_code == status
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]


def test_oversized_upload_is_rejected_before_parsing(client):
    big = b"%PDF-" + b"0" * (settings.max_upload_bytes + 200_000)
    r = upload(client, "big.pdf", big, "application/pdf")
    assert r.status_code == 413
    assert r.headers["x-content-type-options"] == "nosniff"  # the early rejection still gets security headers
    assert "MB" in r.json()["detail"]


def test_oversized_upload_never_reaches_the_parser(client, monkeypatch):
    from medisense_flow.web.routers import documents as router_module

    calls = []
    monkeypatch.setattr(router_module, "extract_text", lambda *a, **k: calls.append(1))
    big = b"%PDF-" + b"0" * (settings.max_upload_bytes + 200_000)
    assert upload(client, "big.pdf", big, "application/pdf").status_code == 413
    assert calls == []          # turned away by its declared size, before the handler ran


def test_handler_enforces_the_limit_even_when_the_declared_size_is_within_slack(client):
    # Just over the limit but inside the multipart slack: passes the early check, caught by the handler.
    data = b"%PDF-" + b"0" * (settings.max_upload_bytes + 1000)
    r = upload(client, "edge.pdf", data, "application/pdf")
    assert r.status_code == 413 and "MB" in r.json()["detail"]


def test_missing_file_field(client):
    assert client.post("/api/documents/extract").status_code == 422


def test_extraction_does_not_start_an_analysis(client):
    before = len(CALLS)
    upload(client, "r.pdf", sf.make_pdf([["some medical text"]]))
    assert len(CALLS) == before


def test_extracted_text_reaches_the_existing_pipeline(client):
    """The whole workflow: upload -> text -> POST /api/sessions (unchanged) -> AnalysisFlow input."""
    extracted = upload(client, "report.docx", sf.make_docx(), DOCX).json()["text"]
    before = len(CALLS)
    created = client.post("/api/sessions", json={"report": extracted})
    assert created.status_code == 202, created.text
    assert len(CALLS) == before + 1
    assert CALLS[-1]["query"] == extracted                       # exactly what the document contained
    view = client.get(f"/api/sessions/{created.json()['session_id']}").json()
    assert view["status"] == "awaiting_answers" and len(view["questions"]) == 3


def test_typed_text_still_works_exactly_as_before(client):
    typed = "I have had a dry cough and a mild fever for four days."
    created = client.post("/api/sessions", json={"report": typed})
    assert created.status_code == 202
    assert CALLS[-1]["query"] == typed


def test_existing_report_length_limit_still_applies(client):
    too_long = "word " * (settings.max_report_chars // 4)
    assert client.post("/api/sessions", json={"report": too_long}).status_code == 422


def test_page_serves_upload_ui(client):
    html = client.get("/").text
    for needle in ('id="report-file"', 'id="dropzone"', 'data-max-upload-mb="1"', 'id="upload-status"'):
        assert needle in html
