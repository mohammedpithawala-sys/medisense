"""Unit tests for web/documents.py (no web server involved)."""
import io
import zipfile

import pytest

from medisense_flow.web.config import settings
from medisense_flow.web.documents import DocumentError, extract_text

from . import sample_files as sf


def fails(filename, data, status=None, contains=""):
    with pytest.raises(DocumentError) as info:
        extract_text(filename, data)
    if status is not None:
        assert info.value.status_code == status
    assert contains.lower() in info.value.message.lower()
    return info.value


# ---------- PDF ----------
def test_pdf_text_is_extracted():
    result = extract_text("report.pdf", sf.make_pdf([["Dry cough for four days.", "Mild fever."]]))
    assert result.kind == "pdf" and result.pages == 1
    assert "Dry cough for four days." in result.text and "Mild fever." in result.text


def test_multi_page_pdf_keeps_page_order():
    pages = [[f"Content of page {n}"] for n in range(1, 5)]
    result = extract_text("multi.PDF", sf.make_pdf(pages))   # extension is case-insensitive
    assert result.pages == 4
    positions = [result.text.index(f"Content of page {n}") for n in range(1, 5)]
    assert positions == sorted(positions)


def test_pdf_table_rows_stay_on_one_line():
    data = sf.make_pdf_table([("Hemoglobin", "11.2 g/dL", "12-16"), ("WBC", "12.5", "4-11")])
    lines = extract_text("labs.pdf", data).text.splitlines()
    assert any("Hemoglobin" in l and "11.2 g/dL" in l and "12-16" in l for l in lines)
    assert any("WBC" in l and "12.5" in l and "4-11" in l for l in lines)


def test_pdf_with_no_text_layer_is_rejected_with_helpful_message():
    fails("scan.pdf", sf.make_blank_pdf(), 422, "scan")


def test_pdf_with_user_password_is_rejected():
    locked = sf.encrypt_pdf(sf.make_pdf([["secret"]]), user_password="hunter2")
    fails("locked.pdf", locked, 422, "password")


def test_pdf_with_only_owner_password_opens():
    # "Encrypted" just to block editing: opens with an empty password, so it should be readable.
    data = sf.encrypt_pdf(sf.make_pdf([["Readable despite encryption"]]), user_password="")
    assert "Readable despite encryption" in extract_text("owner.pdf", data).text


def test_pdf_over_page_limit_is_rejected():
    too_many = sf.make_pdf([[f"p{n}"] for n in range(settings.max_upload_pages + 1)])
    fails("long.pdf", too_many, 422, "pages")


def test_truncated_pdf_is_reported_as_damaged_not_crash():
    data = sf.make_pdf([["hello world"]])
    exc = fails("broken.pdf", data[: len(data) // 2], 422)
    assert "hello world" not in exc.message   # errors never carry document content


# ---------- DOCX ----------
def test_docx_text_headings_lists_and_tables():
    text = extract_text("summary.docx", sf.make_docx()).text
    assert extract_text("summary.docx", sf.make_docx()).pages is None
    assert text.index("Discharge Summary") < text.index("dry cough") < text.index("Findings")
    assert "- Temperature 38.4 C" in text                      # list items
    assert "Hemoglobin | 11.2 g/dL | 12-16" in text            # table row on one line
    assert text.count("Merged heading") == 1                    # merged cell not repeated
    assert "Merged heading | Side note" in text
    assert text.index("WBC | 12.5 | 4-11") < text.index("Follow-up in two weeks.")  # document order


def test_empty_docx_is_rejected():
    fails("empty.docx", sf.make_empty_docx(), 422, "readable text")


def test_docx_that_is_really_a_zip_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("notes.txt", "not a word file")
    fails("fake.docx", buf.getvalue(), 422, "valid DOCX")


def test_docx_with_corrupt_body_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<not valid xml")
    fails("corrupt.docx", buf.getvalue(), 422, "damaged")


def test_zip_bomb_docx_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w/>")
        z.writestr("word/huge.bin", b"\0" * (60 * 1024 * 1024))   # compresses to a few KB
    assert len(buf.getvalue()) < settings.max_upload_bytes
    fails("bomb.docx", buf.getvalue(), 413, "too large")


def test_password_protected_or_legacy_word_file_is_rejected():
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 200
    fails("locked.docx", ole, 422, "password-protected")


# ---------- general validation ----------
@pytest.mark.parametrize("name", ["notes.txt", "photo.png", "sheet.xlsx", "noextension", "", "report.pdf.exe"])
def test_unsupported_extensions(name):
    fails(name, b"hello", 415, "PDF")


def test_legacy_doc_gets_specific_advice():
    fails("old.doc", b"whatever", 415, ".docx")


def test_empty_file():
    fails("a.pdf", b"", 422, "empty")


def test_file_over_size_limit():
    fails("big.pdf", b"%PDF-" + b"0" * settings.max_upload_bytes, 413, f"{settings.max_upload_mb} MB")


def test_wrong_content_for_extension():
    fails("x.pdf", sf.make_docx(), 422, "valid PDF")
    fails("x.docx", sf.make_pdf([["hi"]]), 422, "valid DOCX")
    fails("x.pdf", b"just some text pretending", 422, "valid PDF")


def test_text_over_extracted_limit(monkeypatch):
    from dataclasses import replace

    from medisense_flow.web import documents
    monkeypatch.setattr(documents, "settings", replace(settings, max_extracted_chars=20))
    fails("a.pdf", sf.make_pdf([["x" * 80]]), 422, "very long")


def test_control_characters_and_blank_runs_are_tidied():
    text = extract_text("a.pdf", sf.make_pdf([["Line one"], ["Line two"]])).text
    assert "\x00" not in text and "\n\n\n" not in text
