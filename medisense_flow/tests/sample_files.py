"""Builds small PDF and DOCX files for the tests, so no binary fixtures need to be stored."""
import io

from docx import Document
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def make_pdf(pages: list[list[str]]) -> bytes:
    """A PDF with selectable text: one list of lines per page."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for lines in pages:
        y = 800
        for line in lines:
            c.drawString(60, y, line)
            y -= 18
        c.showPage()
    c.save()
    return buf.getvalue()


def make_pdf_table(rows: list[tuple[str, str, str]]) -> bytes:
    """A lab-style table: each row has three columns drawn at fixed x positions."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for name, value, ref in rows:
        c.drawString(60, y, name)
        c.drawString(250, y, value)
        c.drawString(380, y, ref)
        y -= 20
    c.showPage()
    c.save()
    return buf.getvalue()


def make_blank_pdf(pages: int = 1) -> bytes:
    """A PDF with no text at all (what a scan or a photo of a page looks like to a text extractor)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for _ in range(pages):
        c.rect(50, 50, 100, 100)
        c.showPage()
    c.save()
    return buf.getvalue()


def encrypt_pdf(data: bytes, user_password: str, owner_password: str = "owner-secret") -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(user_password=user_password, owner_password=owner_password, algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def make_docx() -> bytes:
    doc = Document()
    doc.add_heading("Discharge Summary", level=1)
    doc.add_paragraph("Patient reports a dry cough and fever for four days.")
    doc.add_heading("Findings", level=2)
    doc.add_paragraph("Temperature 38.4 C", style="List Bullet")
    doc.add_paragraph("Oxygen saturation 96%", style="List Bullet")
    table = doc.add_table(rows=3, cols=3)
    for r, row in enumerate([("Test", "Result", "Range"), ("Hemoglobin", "11.2 g/dL", "12-16"), ("WBC", "12.5", "4-11")]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    merged = doc.add_table(rows=1, cols=3)
    merged.cell(0, 0).merge(merged.cell(0, 1)).text = "Merged heading"
    merged.cell(0, 2).text = "Side note"
    doc.add_paragraph("Follow-up in two weeks.")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def make_empty_docx() -> bytes:
    out = io.BytesIO()
    Document().save(out)
    return out.getvalue()
