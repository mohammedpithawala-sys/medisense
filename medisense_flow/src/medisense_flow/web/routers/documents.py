"""Read the text out of an uploaded report document.

POST /api/documents/extract    multipart form with one PDF or DOCX file -> {text, kind, pages, characters}

This endpoint only extracts text. It does not start any analysis: the browser puts the text
in the report box so the patient can check it, and the normal POST /api/sessions does the rest.
Signed-in patients only.
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile

from ..auth import current_patient
from ..config import settings
from ..documents import DocumentError, extract_text
from ..schemas import DocumentText

EXTRACT_PATH = "/api/documents/extract"

router = APIRouter(prefix="/api/documents", tags=["documents"], dependencies=[Depends(current_patient)])


@router.post("/extract")
def extract_document(file: UploadFile) -> DocumentText:
    # A plain `def`: FastAPI runs it in its thread pool, so parsing never blocks the server.
    try:
        # Read at most one byte over the limit, so a huge body is never held in memory.
        data = file.file.read(settings.max_upload_bytes + 1)
        result = extract_text(file.filename or "", data)
    except DocumentError as err:
        raise HTTPException(status_code=err.status_code, detail=err.message) from None
    finally:
        file.file.close()  # releases the temporary file Starlette used to receive the upload
    return DocumentText(
        text=result.text,
        kind=result.kind,
        pages=result.pages,
        characters=len(result.text),
    )
