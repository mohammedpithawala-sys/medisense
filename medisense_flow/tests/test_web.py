"""End-to-end tests for the web app. No API key, network or money needed:
only the LLM call (kickoff_with_retry), the Agent class and the RAG tool are
replaced with fakes. Flows, login, database, PDF building, sessions and file
download are all real.

Run:  uv run pytest tests/test_web.py
"""
import os
import re
import tempfile
from datetime import datetime
from types import SimpleNamespace

# Must be set before the app is imported (settings are read at import time).
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ["MEDISENSE_REPORTS_DIR"] = tempfile.mkdtemp(prefix="medisense_reports_")
os.environ["MEDISENSE_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="medisense_db_"), "test.db")

import pytest
from fastapi.testclient import TestClient

import medisense_flow.main as core
from medisense_flow.web import db, pdf_report, runner
from medisense_flow.web.app import app
from medisense_flow.web.auth import logins
from medisense_flow.web.config import settings
from medisense_flow.web.sessions import store

REPORT = "I have had a dry cough and a mild fever for four days."
ANSWERS = ["About four days", "Fever around 38C, no breathing trouble", "No medicines, no allergies"]
PASSWORD = "correct-horse-1"


class FakeAgent:
    def __init__(self, **kwargs):
        self.role = kwargs.get("role", "")


@pytest.fixture(autouse=True)
def fake_ai(monkeypatch):
    """Replace the AI. The fake echoes prompts back, so the final report and the
    database prove the user's text, answers and history really travelled through."""
    prompts: list[tuple[str, str]] = []
    behaviour = {"empty_questions": False, "score": 8, "summary_fails": False, "history_fails": False}
    calls = {"followups": 0}

    def fake_kickoff(agent, prompt, *args, response_format=None, **kwargs):
        prompts.append((agent.role, prompt))
        if response_format is core.FollowUpQuestions:
            calls["followups"] += 1
            if behaviour["empty_questions"]:
                return SimpleNamespace(raw="", pydantic=None)
            q = core.FollowUpQuestions(
                question_1="How long have you had the cough?",
                question_2="How high is your fever?",
                question_3="Do you take any medicines?",
            )
            return SimpleNamespace(raw="questions", pydantic=q)
        if response_format is core.DiagnosisScore:
            if behaviour["score"] is None:
                return SimpleNamespace(raw="", pydantic=None)
            return SimpleNamespace(raw="score", pydantic=core.DiagnosisScore(score=behaviour["score"]))
        if agent.role == "Patient History Analyst":
            if behaviour["history_fails"]:
                raise RuntimeError("history agent exploded")
            return SimpleNamespace(raw="HISTORY-NOTE: " + prompt, pydantic=None)
        if agent.role == "Medical Report Summarizer":
            if behaviour["summary_fails"]:
                raise RuntimeError("summary agent exploded")
            complaint = re.search(r"--- ORIGINAL COMPLAINT ---\n(.*?)\n--- END", prompt, re.S).group(1)
            return SimpleNamespace(raw=f"Summary of: {complaint[:40]}", pydantic=None)
        return SimpleNamespace(raw=prompt, pydantic=None)

    monkeypatch.setattr(core, "kickoff_with_retry", fake_kickoff)
    monkeypatch.setattr(core, "Agent", FakeAgent)
    monkeypatch.setattr(core, "MedicalRAGTool", lambda: None)
    return SimpleNamespace(prompts=prompts, behaviour=behaviour, calls=calls)


def register(client, name="Asha Rao", password=PASSWORD):
    res = client.post("/api/auth/register", json={"name": name, "password": password, "consent": True})
    assert res.status_code == 201, res.text
    return res.json()


@pytest.fixture()
def client():
    """A browser that is already signed in as a brand-new patient."""
    c = TestClient(app)
    c.profile = register(c)
    return c


def start(client, text=REPORT):
    res = client.post("/api/sessions", json={"report": text})
    assert res.status_code == 202, res.text
    return res.json()["session_id"]


def finish(client, text=REPORT, answers=ANSWERS):
    """Run a whole report for the signed-in patient and return (session_id, final view)."""
    sid = start(client, text)
    assert client.post(f"/api/sessions/{sid}/answers", json={"answers": answers}).status_code == 202
    return sid, client.get(f"/api/sessions/{sid}").json()


def prompts_for(fake_ai, role):
    return [p for r, p in fake_ai.prompts if r == role]


# ------------------------------------------------------------------ pages
def test_signed_out_visitor_is_sent_to_the_login_page():
    res = TestClient(app).get("/", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/login"


def test_login_page_and_its_assets_are_served():
    c = TestClient(app)
    page = c.get("/login")
    assert page.status_code == 200
    for text in ("Welcome back", "Create account", "Patient ID", "MediSense", "not a diagnosis or medical advice"):
        assert text in page.text
    assets = ["login.css", "login.js"] + [f"fonts/manrope-latin-{w}-normal.woff2" for w in (400, 500, 600, 700, 800)]
    for name in assets:
        assert c.get(f"/static/{name}").status_code == 200, name
    assert c.get("/static/fonts/manrope-latin-700-normal.woff2").headers["content-type"] == "font/woff2"
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-content-type-options"] == "nosniff"


def test_login_page_loads_nothing_from_other_sites():
    from medisense_flow.web.config import STATIC_DIR, TEMPLATES_DIR
    for path in (TEMPLATES_DIR / "login.html", STATIC_DIR / "login.css", STATIC_DIR / "login.js"):
        assert not re.search(r"https?://", path.read_text(encoding="utf-8")), path.name  # the CSP would block it


def test_login_page_does_not_echo_the_url():
    page = TestClient(app).get("/login?msg=<script>alert(1)</script>")
    assert page.status_code == 200 and "alert(1)" not in page.text


def test_signed_in_patient_gets_the_app_and_skips_the_login_page(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'data-max-report="8000"' in page.text and "Describe your symptoms" in page.text
    assert "panel-auth" not in page.text                      # sign-in now lives on /login
    assert page.headers["cache-control"] == "no-store"
    res = client.get("/login", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/"
    assert client.get("/static/app.js").status_code == 200 and client.get("/static/style.css").status_code == 200


def test_after_sign_out_the_app_page_redirects_again(client):
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.post("/api/auth/logout").status_code == 204
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/login"
    assert client.get("/login").status_code == 200


def test_expired_login_is_sent_back_to_the_login_page(client):
    token = client.cookies.get(settings.cookie_name)
    with logins._lock:
        logins._tokens[token] = (logins._tokens[token][0], 0)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_registering_signs_you_straight_into_the_app():
    c = TestClient(app)
    register(c)
    assert c.get("/", follow_redirects=False).status_code == 200


# ------------------------------------------------------------------ accounts
def test_register_creates_patient_id_and_stores_only_a_password_hash():
    c = TestClient(app)
    profile = register(c, "Ravi Kumar", "my-secret-pass")
    assert re.fullmatch(r"MS-[A-Z2-9]{4}-[A-Z2-9]{4}", profile["patient_code"])
    assert profile["name"] == "Ravi Kumar" and profile["earlier_reports"] == 0
    row = db.get_patient_by_code(profile["patient_code"])
    assert row["name"] == "Ravi Kumar"
    assert "my-secret-pass" not in row["password_hash"] and row["password_hash"].startswith("$argon2")
    assert "httponly" in c.post("/api/auth/login", json={
        "patient_code": profile["patient_code"], "password": "my-secret-pass"}).headers["set-cookie"].lower()


@pytest.mark.parametrize("payload", [
    {"name": "A", "password": PASSWORD, "consent": True},                 # name too short
    {"name": "Asha", "password": "short", "consent": True},               # password too short
    {"name": "Asha", "password": PASSWORD, "consent": False},             # no consent
    {"name": "आशा", "password": PASSWORD, "consent": True},               # not representable in the PDF fonts
    {"name": "Asha\u0007", "password": PASSWORD, "consent": True},        # control character
    {"password": PASSWORD, "consent": True},                              # missing name
])
def test_bad_registrations_are_rejected(payload):
    assert TestClient(app).post("/api/auth/register", json=payload).status_code == 422


def test_login_works_with_any_case_and_spaces_and_wrong_password_fails():
    first = TestClient(app)
    code = register(first)["patient_code"]

    other = TestClient(app)
    ok = other.post("/api/auth/login", json={"patient_code": f"  {code.lower()} ", "password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["patient_code"] == code
    assert other.get("/api/auth/me").json()["name"] == "Asha Rao"

    wrong = TestClient(app).post("/api/auth/login", json={"patient_code": code, "password": "nope-nope-1"})
    unknown = TestClient(app).post("/api/auth/login", json={"patient_code": "MS-AAAA-AAAA", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]  # can't tell which part was wrong


def test_repeated_wrong_passwords_lock_the_patient_id():
    code = register(TestClient(app))["patient_code"]
    c = TestClient(app)
    for _ in range(settings.login_max_failures):
        assert c.post("/api/auth/login", json={"patient_code": code, "password": "wrong-pass-1"}).status_code == 401
    locked = c.post("/api/auth/login", json={"patient_code": code, "password": PASSWORD})  # even the right one
    assert locked.status_code == 429 and "Retry-After" in locked.headers


def test_unknown_ids_are_throttled_the_same_way():
    c = TestClient(app)
    for _ in range(settings.login_max_failures):
        c.post("/api/auth/login", json={"patient_code": "MS-ZZZZ-ZZZZ", "password": "x" * 9})
    assert c.post("/api/auth/login", json={"patient_code": "MS-ZZZZ-ZZZZ", "password": "x" * 9}).status_code == 429


def test_api_requires_login():
    c = TestClient(app)
    assert c.get("/api/auth/me").status_code == 401
    assert c.post("/api/sessions", json={"report": REPORT}).status_code == 401
    assert c.get("/api/sessions/" + "a" * 43).status_code == 401
    assert c.get("/api/sessions/" + "a" * 43 + "/report").status_code == 401


def test_logout_and_expired_login(client):
    assert client.get("/api/auth/me").status_code == 200
    token = client.cookies.get(settings.cookie_name)
    with logins._lock:
        logins._tokens[token] = (logins._tokens[token][0], 0)   # pretend it expired
    assert client.get("/api/auth/me").status_code == 401

    client.post("/api/auth/login", json={"patient_code": client.profile["patient_code"], "password": PASSWORD})
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


# ------------------------------------------------------------------ the report workflow
def test_full_workflow_pdf_summary_and_database(client):
    sid = start(client)
    view = client.get(f"/api/sessions/{sid}").json()
    assert view["status"] == "awaiting_answers" and len(view["questions"]) == 3
    assert view["history_count"] == 0 and view["summary"] is None

    assert client.post(f"/api/sessions/{sid}/answers", json={"answers": ANSWERS}).status_code == 202
    view = client.get(f"/api/sessions/{sid}").json()
    assert view["status"] == "complete" and view["score"] == 8
    assert view["summary"].startswith("Summary of: I have had a dry cough")

    pdf = client.get(view["download_url"])
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF") and pdf.content.rstrip().endswith(b"%%EOF")
    disposition = pdf.headers["content-disposition"]
    assert "attachment" in disposition and f"MediSense_Report_Asha_Rao_{datetime.now():%Y-%m-%d}.pdf" in disposition

    saved = db.get_recent_reports(db.get_patient_by_code(client.profile["patient_code"])["id"], 5)
    assert len(saved) == 1 and saved[0]["summary"] == view["summary"] and saved[0]["score"] == 8


def test_the_ai_never_receives_the_patient_name_or_id(client, fake_ai):
    finish(client)
    everything = " ".join(p for _, p in fake_ai.prompts)
    assert "Asha" not in everything and client.profile["patient_code"] not in everything


def test_history_reaches_the_agents_only_for_returning_patients(client, fake_ai):
    # First visit: no history agent, and the prompts contain no history block.
    finish(client, "My knee hurts after running for the last two weeks.")
    assert prompts_for(fake_ai, "Patient History Analyst") == []
    assert all("PATIENT HISTORY NOTE" not in p for p in prompts_for(fake_ai, "Medical Diagnostic Follow-up Specialist"))
    assert client.get("/api/auth/me").json()["earlier_reports"] == 1

    # Second visit: the history agent sees the first summary and its note reaches the other agents.
    fake_ai.prompts.clear()
    sid = start(client, "My knee is swollen again this morning.")
    view = client.get(f"/api/sessions/{sid}").json()
    assert view["history_count"] == 1
    history_prompt = prompts_for(fake_ai, "Patient History Analyst")[0]
    assert "Summary of: My knee hurts after running" in history_prompt
    assert "My knee is swollen again" in history_prompt
    assert "PATIENT HISTORY NOTE" in prompts_for(fake_ai, "Medical Diagnostic Follow-up Specialist")[0]

    client.post(f"/api/sessions/{sid}/answers", json={"answers": ANSWERS})
    assert "PATIENT HISTORY NOTE" in prompts_for(fake_ai, "Medical Re-Diagnosis Specialist")[0]
    assert "HISTORY-NOTE" in prompts_for(fake_ai, "Medical Report Summarizer")[0]


def test_one_patient_never_sees_another_patients_history_or_sessions(fake_ai):
    a, b = TestClient(app), TestClient(app)
    register(a, "Patient A"), register(b, "Patient B")
    sid_a, view_a = finish(a, "Patient A has a very particular headache pattern.")

    fake_ai.prompts.clear()
    sid_b = start(b, "Patient B has a sore throat since yesterday evening.")
    assert prompts_for(fake_ai, "Patient History Analyst") == []        # B has no history of their own
    assert all("particular headache" not in p for _, p in fake_ai.prompts)

    assert b.get(f"/api/sessions/{sid_a}").status_code == 404           # can't read A's session...
    assert b.get(view_a["download_url"]).status_code == 404             # ...or download A's PDF
    assert b.post(f"/api/sessions/{sid_a}/answers", json={"answers": ANSWERS}).status_code == 404
    assert a.get(f"/api/sessions/{sid_b}").status_code == 404


@pytest.mark.parametrize("payload", [{}, {"report": ""}, {"report": "short"}, {"report": "x" * 8001}])
def test_bad_report_is_rejected(client, payload):
    assert client.post("/api/sessions", json=payload).status_code == 422


def test_answers_must_be_exactly_three_and_non_empty(client):
    sid = start(client)
    for bad in (["a", "b"], ["a", "b", "c", "d"], ["a", "", "c"], ["a", "   ", "c"]):
        assert client.post(f"/api/sessions/{sid}/answers", json={"answers": bad}).status_code == 422
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "awaiting_answers"


def test_unknown_and_malformed_session_ids(client):
    assert client.get("/api/sessions/" + "a" * 43).status_code == 404
    assert client.get("/api/sessions/short").status_code == 422
    assert client.get("/api/sessions/" + "%2e%2e%2f" * 15 + "etc%2fpasswd").status_code in (404, 422)


def test_answers_blocked_until_questions_exist_and_double_submit(client):
    me = client.get("/api/auth/me").json()
    pending = store.create(REPORT, patient_id=db.get_patient_by_code(me["patient_code"])["id"],
                           patient_name=me["name"], patient_code=me["patient_code"])
    assert client.post(f"/api/sessions/{pending.id}/answers", json={"answers": ANSWERS}).status_code == 409

    sid = start(client)
    assert client.post(f"/api/sessions/{sid}/answers", json={"answers": ANSWERS}).status_code == 202
    assert client.post(f"/api/sessions/{sid}/answers", json={"answers": ANSWERS}).status_code == 409


def test_download_before_ready_is_404(client):
    sid = start(client)
    assert client.get(f"/api/sessions/{sid}/report").status_code == 404


def test_empty_questions_are_retried_then_fail_cleanly(client, fake_ai):
    fake_ai.behaviour["empty_questions"] = True
    sid = start(client)
    view = client.get(f"/api/sessions/{sid}").json()
    assert view["status"] == "failed" and view["error"] == runner.USER_FACING_ERROR
    assert fake_ai.calls["followups"] == runner.QUESTION_ATTEMPTS


def test_failed_scoring_shows_as_not_scored(client, fake_ai):
    fake_ai.behaviour["score"] = None
    _, view = finish(client)
    assert view["status"] == "complete" and view["score"] is None


def test_failed_summary_agent_still_delivers_the_report(client, fake_ai):
    fake_ai.behaviour["summary_fails"] = True
    sid, view = finish(client)
    assert view["status"] == "complete"
    assert "could not be generated" in view["summary"]
    assert client.get(view["download_url"]).content.startswith(b"%PDF")


def test_failed_history_agent_does_not_block_the_analysis(client, fake_ai):
    finish(client)                                   # creates history
    fake_ai.behaviour["history_fails"] = True
    sid = start(client, "A completely new complaint about my left ankle.")
    view = client.get(f"/api/sessions/{sid}").json()
    assert view["status"] == "awaiting_answers"      # carried on without the history note
    assert all("PATIENT HISTORY NOTE" not in p for p in prompts_for(fake_ai, "Medical Diagnostic Follow-up Specialist")[-1:])


def test_crewai_crash_marks_session_failed_without_leaking_details(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider exploded: secret details")
    monkeypatch.setattr(core, "kickoff_with_retry", boom)
    sid = start(client)
    view = client.get(f"/api/sessions/{sid}").json()
    assert view["status"] == "failed" and "secret" not in (view["error"] or "")


def test_database_failure_does_not_block_the_pdf(client, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "save_report", broken)
    _, view = finish(client)
    assert view["status"] == "complete"
    assert client.get(view["download_url"]).content.startswith(b"%PDF")


def test_pdf_failure_marks_session_failed(client, monkeypatch):
    monkeypatch.setattr(runner, "build_pdf", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad pdf")))
    _, view = finish(client)
    assert view["status"] == "failed" and view["error"] == runner.USER_FACING_ERROR


def test_download_refuses_files_outside_reports_dir(client, tmp_path):
    outside = tmp_path / "secret.pdf"
    outside.write_text("do not serve me")
    sid, _ = finish(client)
    store.update(sid, report_path=outside)
    assert client.get(f"/api/sessions/{sid}/report").status_code == 404


def test_expired_sessions_and_files_are_purged_but_saved_reports_stay(client):
    sid, _ = finish(client)
    folder = settings.reports_dir / sid
    assert folder.is_dir()
    with store._lock:
        store._items[sid].updated_at = 0      # pretend it is very old
    store.purge_expired()
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert not folder.exists()
    assert client.get("/api/auth/me").json()["earlier_reports"] == 1   # the database copy is kept


# ------------------------------------------------------------------ delete my data
def test_delete_account_removes_everything(client):
    code = client.profile["patient_code"]
    pid = db.get_patient_by_code(code)["id"]
    sid, _ = finish(client)
    folder = settings.reports_dir / sid
    assert folder.is_dir() and db.count_reports(pid) == 1

    assert client.post("/api/auth/delete-account", json={"password": "wrong-pass-1"}).status_code == 403
    assert client.get("/api/auth/me").status_code == 200                 # still signed in and intact

    assert client.post("/api/auth/delete-account", json={"password": PASSWORD}).status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    assert db.get_patient_by_code(code) is None and db.count_reports(pid) == 0
    assert not folder.exists()
    assert TestClient(app).post("/api/auth/login", json={"patient_code": code, "password": PASSWORD}).status_code == 401


# ------------------------------------------------------------------ PDF builder
def _data(**over):
    base = dict(
        patient_name="Asha Rao", patient_code="MS-ABCD-2345", report_id="1A2B3C4D", created=datetime(2026, 11, 15),
        submitted_report="Dry cough & fever > 38C for 4 days.\n\nSecond paragraph.",
        initial_assessment="## Possible conditions\n- **Common cold** (likely)\n- *Influenza*\n\n1. Rest\n2. Fluids",
        qa=[("How long?", "4 days"), ("How high?", "38C"), ("Medicines?", "None")],
        final_assessment="Most likely a viral infection. See a doctor if breathing gets harder.",
        summary="Your symptoms point to a viral infection. Rest and see a doctor if it gets worse.", score=7,
    )
    base.update(over)
    return pdf_report.ReportData(**base)


def test_pdf_builder_survives_hostile_and_unusual_text(tmp_path):
    nasty = ("<script>alert(1)</script> & <b>unclosed **bold *nested** text* `code` \u2192 \u2265 \U0001F600 "
             "\u0939\u093f\u0928\u094d\u0926\u0940 \x00\x07 [link](http://x.y) </para> <font")
    out = tmp_path / "nasty.pdf"
    pdf_report.build_pdf(out, _data(initial_assessment=nasty, final_assessment=nasty, submitted_report=nasty,
                                    history_note=nasty, summary=nasty,
                                    qa=[(nasty, nasty), ("Q2", ""), ("Q3", "x" * 3000)]))
    assert out.read_bytes().startswith(b"%PDF")
    assert not list(tmp_path.glob("*.tmp"))


def test_pdf_builder_handles_long_reports_and_missing_score(tmp_path):
    long_text = "\n".join(f"- Finding number {i}: " + "word " * 40 for i in range(120))
    out = tmp_path / "long.pdf"
    pdf_report.build_pdf(out, _data(initial_assessment=long_text, final_assessment=long_text, score=None))
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") >= 3


def test_clean_text_maps_symbols_and_drops_unsupported_characters():
    assert pdf_report.clean_text("a \u2192 b \u2265 c") == "a -> b >= c"
    assert pdf_report.clean_text("fever \U0001F600 high") == "fever  high"
    assert pdf_report.clean_text("na\u00efve caf\u00e9 \u2013 ok") == "na\u00efve caf\u00e9 \u2013 ok"


# ------------------------------------------------------------------ the terminal flow is unchanged
def test_original_terminal_flow_still_works(monkeypatch, tmp_path, fake_ai):
    monkeypatch.chdir(tmp_path)
    typed = iter([REPORT, "END", "ans one", "ans two", "ans three"])
    monkeypatch.setattr("builtins.input", lambda *a: next(typed))
    flow = core.ContentFlow()
    path = flow.kickoff()
    assert path.startswith("diagnosis_report_") and path.endswith(".txt")
    content = (tmp_path / path).read_text(encoding="utf-8")
    assert REPORT in content and "ans two" in content and flow.state.score == 8
    assert all("PATIENT HISTORY NOTE" not in p for _, p in fake_ai.prompts)
