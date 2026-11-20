"""Turn an uploaded PDF or Word (.docx) file into plain text.

This module only reads bytes and returns text. It knows nothing about FastAPI,
sessions or the AI: the text it returns is shown to the patient in the report box,
and once they press "Analyze report" it travels through exactly the same path as
text they typed (POST /api/sessions -> run_analysis -> AnalysisFlow).

Safety notes
- Files are parsed from memory. Nothing is written to disk, so there is no
  temporary file to clean up or leak.
- The file type is decided by its content (magic bytes), not by the extension or the
  browser's Content-Type, both of which the sender controls.
- Size, page count and DOCX decompressed size are limited before any text is built.
- Errors never include the document's text, and nothing in here logs it.
"""
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass

from .config import settings

PDF_EXTENSION = ".pdf"
DOCX_EXTENSION = ".docx"

# A .docx is a zip. Refuse "zip bombs": a tiny file that expands to something huge.
_MAX_DOCX_UNZIPPED_BYTES = 50 * 1024 * 1024
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # old .doc files and password-protected .docx files


class DocumentError(Exception):
    """A problem the patient can understand. `message` is safe to show; `status_code` is the HTTP status."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    kind: str            # "pdf" or "docx"
    pages: int | None    # PDFs only


# ---------- public entry point ----------
def extract_text(filename: str, data: bytes) -> ExtractedDocument:
    """Validate the file and return its text. Raises DocumentError for anything the patient can fix."""
    name = (filename or "").lower()
    if name.endswith(PDF_EXTENSION):
        expected = "pdf"
    elif name.endswith(DOCX_EXTENSION):
        expected = "docx"
    elif name.endswith(".doc"):
        raise DocumentError(
            "Old Word (.doc) files aren't supported. In Word, choose Save As and pick "
            "Word Document (.docx), then upload that.",
            415,
        )
    else:
        raise DocumentError("Only PDF (.pdf) and Word (.docx) files can be uploaded.", 415)

    if not data:
        raise DocumentError("This file is empty.")
    if len(data) > settings.max_upload_bytes:
        raise DocumentError(f"This file is larger than {settings.max_upload_mb} MB.", 413)

    actual = _sniff(data)
    if actual == "ole":
        raise DocumentError(
            "This Word file is password-protected or saved in the old .doc format. "
            "Remove the password or save it as a .docx file, then upload it again."
        )
    if actual != expected:
        raise DocumentError(
            f"This file doesn't look like a valid {expected.upper()} document. "
            "It may be damaged or have the wrong file extension."
        )

    if expected == "pdf":
        text, pages = _read_pdf(data)
    else:
        text, pages = _read_docx(data), None

    text = _tidy(text)
    if not text:
        if expected == "pdf":
            raise DocumentError(
                "No readable text was found. This PDF may be a scan or a photo of a page. "
                "Please upload a PDF with selectable text, or type or paste the report instead."
            )
        raise DocumentError("This document doesn't contain any readable text.")
    if len(text) > settings.max_extracted_chars:
        raise DocumentError(
            f"This document is very long ({len(text):,} characters). "
            "Please upload only the relevant pages, or paste the key parts of the report."
        )
    return ExtractedDocument(text=text, kind=expected, pages=pages)


# ---------- file type detection ----------
def _sniff(data: bytes) -> str:
    """Return 'pdf', 'docx', 'ole' (old Word / encrypted Office) or 'unknown' from the file's first bytes."""
    if b"%PDF-" in data[:1024]:  # the PDF spec lets the header sit within the first 1 KB
        return "pdf"
    if data.startswith(_OLE_MAGIC):
        return "ole"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile:
            return "unknown"
        if "[Content_Types].xml" in names and "word/document.xml" in names:
            return "docx"
    return "unknown"


# ---------- PDF ----------
def _read_pdf(data: bytes) -> tuple[str, int]:
    from pypdf import PdfReader
    from pypdf.errors import DependencyError

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            # Many PDFs are "encrypted" only to block editing and open with an empty password.
            if not reader.decrypt(""):
                raise DocumentError(
                    "This PDF is password-protected. Remove the password and upload it again, "
                    "or type or paste the report instead."
                )
        page_count = len(reader.pages)
        if page_count == 0:
            raise DocumentError("This PDF has no pages.")
        if page_count > settings.max_upload_pages:
            raise DocumentError(
                f"This PDF has {page_count} pages. The limit is {settings.max_upload_pages}. "
                "Please upload only the relevant pages."
            )
        parts = []
        for page in reader.pages:
            # "layout" mode keeps table rows (e.g. lab results) on one line instead of
            # splitting every cell onto its own line.
            parts.append(page.extract_text(extraction_mode="layout") or "")
        return "\n\n".join(parts), page_count
    except DocumentError:
        raise
    except DependencyError:
        raise DocumentError("This PDF uses encryption that can't be read. Please upload an unprotected copy.")
    except Exception:
        # Malformed files raise many different pypdf/ValueError/Recursion errors. The
        # patient only needs to know it can't be read; the message never carries file content.
        raise DocumentError("This PDF is damaged or can't be read. Please try another copy.") from None


# ---------- DOCX ----------
def _read_docx(data: bytes) -> str:
    from docx import Document

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(info.file_size for info in archive.infolist()) > _MAX_DOCX_UNZIPPED_BYTES:
                raise DocumentError("This Word document is too large to read.", 413)
        document = Document(io.BytesIO(data))
        lines = _docx_blocks(document)
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("This Word document is damaged or can't be read. Please try another copy.") from None
    return "\n".join(lines)


def _docx_blocks(parent, depth: int = 0) -> list[str]:
    """Paragraphs and tables in the order they appear in the document."""
    from docx.table import Table

    lines: list[str] = []
    for block in parent.iter_inner_content():
        if isinstance(block, Table):
            lines.extend(_docx_table(block, depth))
            continue
        text = block.text.strip()
        if not text:
            lines.append("")  # keep paragraph breaks
            continue
        style = (block.style.name if block.style is not None else "") or ""
        if style == "Title" or style.startswith("Heading"):
            lines.extend(["", text, ""])
        elif style.startswith("List"):
            lines.append(f"- {text}")
        else:
            lines.append(text)
    return lines


def _docx_table(table, depth: int) -> list[str]:
    """One line per row, cells separated by ' | '. Merged cells are only listed once."""
    if depth > 3:  # absurdly nested tables: stop rather than recurse forever
        return []
    rows: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        seen: set[int] = set()
        for cell in row.cells:
            if id(cell._tc) in seen:  # a merged cell shows up once per column it spans
                continue
            seen.add(id(cell._tc))
            inner = [line.strip() for line in _docx_blocks(cell, depth + 1) if line.strip()]
            cells.append(" ".join(inner))
        if any(cells):
            rows.append(" | ".join(cells))
    return ["", *rows, ""] if rows else []


# ---------- cleanup ----------
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LONG_GAPS = re.compile(r"[ \t]{3,}")
_BLANK_RUNS = re.compile(r"\n{3,}")


def _tidy(text: str) -> str:
    """Normalise line endings and spacing so the text reads well in the report box."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = _CONTROL_CHARS.sub("", text)
    text = "\n".join(_LONG_GAPS.sub("   ", line.strip()) if line.strip() else "" for line in text.split("\n"))
    return _BLANK_RUNS.sub("\n\n", text).strip()
