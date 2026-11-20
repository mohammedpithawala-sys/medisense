"""Test setup: private temp storage, and a stand-in for the CrewAI flows.

The stand-in (`FakeAnalysisFlow`) only RECORDS what it is given, so the tests can prove that text
extracted from a document reaches the same `AnalysisFlow` input as typed text. The real AI code is not
changed or replaced in the app. Set MEDISENSE_TEST_REAL_CREW=1 to import the real flows instead.
"""
import os
import sys
import tempfile
import types
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="medisense-tests-"))
os.environ["MEDISENSE_DB_PATH"] = str(_tmp / "test.db")
os.environ["MEDISENSE_REPORTS_DIR"] = str(_tmp / "reports")
os.environ["MEDISENSE_MAX_UPLOAD_MB"] = "1"        # keeps the "too large" test small
os.environ["MEDISENSE_MAX_UPLOAD_PAGES"] = "5"

CALLS: list[dict] = []   # every `inputs` dict passed to AnalysisFlow.kickoff


class _State:
    q1, q2, q3 = "Question one?", "Question two?", "Question three?"
    diagnostics_init = "initial assessment"
    history_note = ""


class FakeAnalysisFlow:
    def kickoff(self, inputs):
        CALLS.append(dict(inputs))
        self.state = _State()


class FakeReportFlow:
    def kickoff(self, inputs):  # not used by these tests
        raise AssertionError("ReportFlow should not run in these tests")


if not os.getenv("MEDISENSE_TEST_REAL_CREW"):
    fake = types.ModuleType("medisense_flow.main")
    fake.AnalysisFlow = FakeAnalysisFlow
    fake.ReportFlow = FakeReportFlow
    sys.modules["medisense_flow.main"] = fake
