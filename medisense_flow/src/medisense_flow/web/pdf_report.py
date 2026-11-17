"""Builds the downloadable PDF report with ReportLab.

Layout: a colour band with the patient details, a highlighted summary and score
bar, then the sections (history, what was reported, initial assessment, the
follow-up questions and answers, updated assessment) and a footer on every page
with the safety note and "Page X of Y".

ReportLab's built-in fonts only cover Latin text (accents are fine). Anything
else the AI writes (arrows, emoji, ...) is mapped or dropped by `clean_text`
so it never shows up as black boxes."""
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    KeepTogether,
    ListFlowable,
    ListItem,
    NextPageTemplate,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

# ---- palette (matches the web page) ----
INK = colors.HexColor("#10262B")
PINE = colors.HexColor("#145C52")
MUTED = colors.HexColor("#4B6366")
LINE = colors.HexColor("#C9D8D6")
TINT = colors.HexColor("#EEF5F3")
BAND_SUB = colors.HexColor("#CFE5E0")
CAUTION_BG = colors.HexColor("#FBF3DF")
CAUTION_INK = colors.HexColor("#6B4A00")

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm
CONTENT_W = PAGE_W - 2 * MARGIN
BAND_H = 30 * mm
FOOTER_Y = 12 * mm

DISCLAIMER = (
    "This report is general information produced by an AI system. It is not a diagnosis "
    "or medical advice. If your symptoms are severe or getting worse, contact a doctor "
    "or your local emergency number."
)


@dataclass
class ReportData:
    patient_name: str
    patient_code: str
    report_id: str
    created: datetime
    submitted_report: str
    initial_assessment: str
    qa: list[tuple[str, str]]
    final_assessment: str
    summary: str
    score: int | None = None
    history_note: str = ""


# ---------------------------------------------------------------- text cleanup
_SYMBOLS = {
    "\u2192": "->", "\u2190": "<-", "\u2194": "<->", "\u21d2": "=>",
    "\u2265": ">=", "\u2264": "<=", "\u2248": "~", "\u2260": "!=", "\u2212": "-",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2022": "\u2022",
    "\u2713": "-", "\u2714": "-", "\u2717": "x", "\u2718": "x",
    "\u00a0": " ", "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
    "\u2032": "'", "\u2033": '"',
}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(text: str) -> str:
    """Make text safe for the built-in PDF fonts (Latin text only)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for old, new in _SYMBOLS.items():
        text = text.replace(old, new)
    text = _CONTROL.sub("", text)
    return text.encode("cp1252", "ignore").decode("cp1252")


# ---------------------------------------------------------------- styles
def _styles() -> dict[str, ParagraphStyle]:
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=15, textColor=INK, spaceAfter=5)
    return {
        "body": body,
        "muted": ParagraphStyle("muted", parent=body, fontSize=8.5, leading=11, textColor=MUTED, spaceAfter=0),
        "value": ParagraphStyle("value", parent=body, fontName="Helvetica-Bold", fontSize=10, leading=13, spaceAfter=0),
        "h2": ParagraphStyle("h2", fontName="Times-Bold", fontSize=15, leading=19, textColor=PINE,
                             spaceBefore=0, spaceAfter=0),
        "h3": ParagraphStyle("h3", parent=body, fontName="Helvetica-Bold", fontSize=10.5, leading=14,
                             spaceBefore=8, spaceAfter=3, keepWithNext=1),
        "q": ParagraphStyle("q", parent=body, fontName="Helvetica-Bold", textColor=PINE, spaceBefore=6, spaceAfter=2,
                            keepWithNext=1),
        "a": ParagraphStyle("a", parent=body, leftIndent=10, spaceAfter=6),
        "summary": ParagraphStyle("summary", parent=body, fontSize=11, leading=16.5, spaceAfter=0),
        "caution": ParagraphStyle("caution", parent=body, fontSize=9, leading=13, textColor=CAUTION_INK, spaceAfter=0),
    }


# ---------------------------------------------------------------- markdown -> flowables
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline(text: str) -> str:
    """Escape XML characters, then turn a small subset of markdown into ReportLab tags."""
    text = _LINK.sub(r"\1 (\2)", text)
    text = escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?![\*\w])", r"<i>\1</i>", text)
    text = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', text)
    return text


def _para(text: str, style: ParagraphStyle) -> Paragraph:
    try:
        return Paragraph(_inline(text), style)
    except Exception:  # badly nested tags from the AI: fall back to plain text
        return Paragraph(escape(text), style)


def md_to_flowables(text: str, styles: dict[str, ParagraphStyle]) -> list[Flowable]:
    """Paragraphs, headings (#), bullets (- * bullet) and numbered lists (1.) become real PDF elements."""
    flow: list[Flowable] = []
    para: list[str] = []
    items: list[str] = []
    kind = ""        # "bullet" or "number" while a list is open
    first_num = 1

    def flush_para() -> None:
        if para:
            flow.append(_para(" ".join(para), styles["body"]))
            para.clear()

    def flush_list() -> None:
        nonlocal kind
        if items:
            list_items = [ListItem(_para(t, styles["body"]), leftIndent=16) for t in items]
            if kind == "bullet":
                flow.append(ListFlowable(list_items, bulletType="bullet", start="\u2022", leftIndent=16,
                                         bulletColor=PINE, bulletFontSize=10))
            else:
                flow.append(ListFlowable(list_items, bulletType="1", bulletFormat="%s.", start=first_num,
                                         leftIndent=18, bulletColor=PINE, bulletFontName="Helvetica-Bold",
                                         bulletFontSize=10))
            items.clear()
        kind = ""

    for raw in clean_text(text).split("\n"):
        line = raw.strip()
        if not line:
            flush_para(); flush_list()
            continue
        if re.fullmatch(r"[-*_=]{3,}", line):          # horizontal rules are dropped
            flush_para(); flush_list()
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if heading:
            flush_para(); flush_list()
            flow.append(_para(heading.group(1).strip(" #"), styles["h3"]))
            continue
        bullet = re.match(r"^[-*\u2022]\s+(.*)$", line)
        number = re.match(r"^(\d+)[.)]\s+(.*)$", line)
        if bullet or number:
            flush_para()
            new_kind = "bullet" if bullet else "number"
            if kind and kind != new_kind:
                flush_list()
            if not kind:
                kind = new_kind
                first_num = int(number.group(1)) if number else 1
            items.append((bullet or number).group(bullet.lastindex if bullet else 2))
            continue
        if items and raw.startswith((" ", "\t")):     # wrapped line belonging to the last list item
            items[-1] += " " + line
            continue
        flush_list()
        para.append(line)
    flush_para(); flush_list()
    return flow


def plain_paragraphs(text: str, style: ParagraphStyle) -> list[Flowable]:
    """For text the user typed: keep their paragraphs and line breaks, show symbols as typed."""
    out: list[Flowable] = []
    for block in clean_text(text).split("\n\n"):
        block = block.strip()
        if block:
            out.append(Paragraph(escape(block).replace("\n", "<br/>"), style))
    return out


# ---------------------------------------------------------------- custom elements
class ScoreBar(Flowable):
    """A label and a horizontal bar showing the self-check score out of 10."""

    def __init__(self, score: int, width: float) -> None:
        super().__init__()
        self.score = max(0, min(10, int(score)))
        self.width = width
        self.height = 24

    def wrap(self, avail_w, avail_h):
        return self.width, self.height

    def draw(self) -> None:
        c = self.canv
        bar_w = min(self.width, 90 * mm)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 9)
        c.drawString(0, 15, f"Self-check score: {self.score} out of 10")
        c.setFillColor(LINE)
        c.roundRect(0, 0, bar_w, 7, 3.5, stroke=0, fill=1)
        if self.score:
            c.setFillColor(CAUTION_INK if self.score <= 4 else PINE)
            c.roundRect(0, 0, bar_w * self.score / 10, 7, 3.5, stroke=0, fill=1)


def _boxed(flowables, background, accent) -> Table:
    table = Table([[flowables]], colWidths=[CONTENT_W])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), background),
        ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    return table


def _heading(title: str, styles) -> Table:
    """Section title with a thin rule under it. It is a one-cell table (so the rule belongs to
    it) marked keepWithNext, which keeps it on the same page as whatever follows."""
    table = Table([[Paragraph(escape(title), styles["h2"])]], colWidths=[CONTENT_W])
    table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.6, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    table.spaceBefore = 16
    table.spaceAfter = 8
    table.keepWithNext = 1
    return table


def _section(title: str, styles, content: list[Flowable]) -> list[Flowable]:
    return [_heading(title, styles)] + content


# ---------------------------------------------------------------- page decoration
class _NumberedCanvas(pdfcanvas.Canvas):
    """Canvas that knows the total page count, so the footer can say 'Page 2 of 3'."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved: list[dict] = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total: int) -> None:
        self.setStrokeColor(LINE)
        self.setLineWidth(0.6)
        self.line(MARGIN, FOOTER_Y + 5 * mm, PAGE_W - MARGIN, FOOTER_Y + 5 * mm)
        self.setFillColor(MUTED)
        self.setFont("Helvetica", 7.5)
        self.drawString(MARGIN, FOOTER_Y + 1 * mm, "General information only. Not a diagnosis or medical advice.")
        self.drawRightString(PAGE_W - MARGIN, FOOTER_Y + 1 * mm, f"Page {self._pageNumber} of {total}")


def _draw_first_page(date_text: str):
    def draw(canv, doc):
        canv.saveState()
        canv.setFillColor(PINE)
        canv.rect(0, PAGE_H - BAND_H, PAGE_W, BAND_H, stroke=0, fill=1)
        canv.setFillColor(colors.white)
        canv.setFont("Times-Bold", 24)
        canv.drawString(MARGIN, PAGE_H - 15 * mm, "MediSense")
        canv.setFillColor(BAND_SUB)
        canv.setFont("Helvetica", 10)
        canv.drawString(MARGIN, PAGE_H - 22 * mm, "Symptom review report")
        canv.drawRightString(PAGE_W - MARGIN, PAGE_H - 22 * mm, date_text)
        canv.restoreState()
    return draw


def _draw_later_page(patient_name: str):
    def draw(canv, doc):
        canv.saveState()
        canv.setFillColor(PINE)
        canv.setFont("Times-Bold", 11)
        canv.drawString(MARGIN, PAGE_H - 12 * mm, "MediSense")
        canv.setFillColor(MUTED)
        canv.setFont("Helvetica", 8.5)
        canv.drawRightString(PAGE_W - MARGIN, PAGE_H - 12 * mm, patient_name)
        canv.setStrokeColor(LINE)
        canv.setLineWidth(0.6)
        canv.line(MARGIN, PAGE_H - 15 * mm, PAGE_W - MARGIN, PAGE_H - 15 * mm)
        canv.restoreState()
    return draw


# ---------------------------------------------------------------- the report
def build_pdf(path: Path, data: ReportData) -> None:
    """Write the PDF to `path` (written to a temporary file first, then moved into place)."""
    styles = _styles()
    name = clean_text(data.patient_name).strip() or "Patient"
    date_text = f"{data.created.day} {data.created:%b %Y}"

    # --- patient details ---
    def cell(label: str, value: str) -> list[Flowable]:
        return [Paragraph(escape(label), styles["muted"]), Paragraph(escape(clean_text(value)), styles["value"])]

    details = Table(
        [[cell("Patient", name), cell("Patient ID", data.patient_code)],
         [cell("Date", date_text), cell("Report ID", data.report_id)]],
        colWidths=[CONTENT_W * 0.55, CONTENT_W * 0.45],
    )
    details.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, -1), (-1, -1), 0.6, LINE),
    ]))

    story: list = [NextPageTemplate("later"), details]

    # --- summary + score ---
    summary_flow = plain_paragraphs(data.summary, styles["summary"]) or [Paragraph("-", styles["summary"])]
    story += [_heading("Summary", styles), _boxed(summary_flow, TINT, PINE)]
    if data.score is not None:
        story += [Spacer(1, 10), ScoreBar(data.score, CONTENT_W)]

    # --- sections ---
    if data.history_note.strip():
        story += _section("Relevant history", styles, md_to_flowables(data.history_note, styles))
    story += _section("What you reported", styles, plain_paragraphs(data.submitted_report, styles["body"]))
    story += _section("Initial assessment", styles, md_to_flowables(data.initial_assessment, styles))

    qa_flow: list[Flowable] = []
    for number, (question, answer) in enumerate(data.qa, start=1):
        qa_flow.append(_para(f"{number}. {clean_text(question)}", styles["q"]))
        qa_flow.extend(plain_paragraphs(answer, styles["a"]) or [Paragraph("-", styles["a"])])
    story += _section("Your answers to the follow-up questions", styles, qa_flow)
    story += _section("Updated assessment", styles, md_to_flowables(data.final_assessment, styles))

    story += [Spacer(1, 18),
              KeepTogether([_boxed([Paragraph(escape(DISCLAIMER), styles["caution"])], CAUTION_BG, CAUTION_INK)])]

    # --- build ---
    first_frame = Frame(MARGIN, 20 * mm, CONTENT_W, PAGE_H - BAND_H - 8 * mm - 20 * mm,
                        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="first")
    later_frame = Frame(MARGIN, 20 * mm, CONTENT_W, PAGE_H - 22 * mm - 20 * mm,
                        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="later")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    doc = BaseDocTemplate(
        str(tmp), pagesize=A4, title=f"MediSense report - {name}", author="MediSense",
        subject="Symptom review report", leftMargin=MARGIN, rightMargin=MARGIN,
    )
    doc.addPageTemplates([
        PageTemplate(id="first", frames=[first_frame], onPage=_draw_first_page(date_text)),
        PageTemplate(id="later", frames=[later_frame], onPage=_draw_later_page(name)),
    ])
    doc.build(story, canvasmaker=_NumberedCanvas)
    os.replace(tmp, path)
