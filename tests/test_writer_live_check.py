import json
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.cli import main
from sira.code_writer import CodeModelBatch, ResearchBackedCodeWriter


class FakeCodeModel:
    name = "fake_code_model"
    model_id = "fake-live-check"

    def __init__(self, data):
        self.data = data
        self.calls = 0

    def generate_patch(self, payload, schema):
        self.calls += 1
        return CodeModelBatch(self.data, api_requests=1, input_tokens=11, output_tokens=7)


class PassingRunner:
    def evaluate(self, root, benchmark_suite):
        return {
            "overall_passed": True,
            "tests": {"passed": True, "test_count": 1},
            "benchmark": {"passed": True, "passed_cases": 1, "failed_cases": 0},
        }


class WriterLiveCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src/sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "src/sira/sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "tests/test_sample.py").write_text("# sample\n", encoding="utf-8")
        (self.root / "benchmarks/improvement_cases.json").write_text("[]\n", encoding="utf-8")
        (self.root / "README.md").write_text("SIRA\n", encoding="utf-8")
        (self.root / ".env.example").write_text("GEMINI_API_KEY=\n", encoding="utf-8")
        (self.root / "sira.py").write_text("# entry\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_live_check_exercises_writer_and_both_evaluators_without_promotion(self):
        from sira.writer_live_check import LIVE_CHECK_PATH, LIVE_CHECK_MARKER, run_writer_live_check

        model = FakeCodeModel({
            "summary": "Create the requested isolated diagnostic document.",
            "edits": [{
                "path": LIVE_CHECK_PATH,
                "content": f"# SIRA Writer Live Check\n\n{LIVE_CHECK_MARKER}\n",
                "reason": "Harmless isolated writer diagnostic.",
            }],
        })
        writer = ResearchBackedCodeWriter(self.root, model)
        before = (self.root / "src/sira/sample.py").read_text(encoding="utf-8")

        result = run_writer_live_check(self.root, writer=writer, runner=PassingRunner())

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["writer_status"], "candidate_prepared")
        self.assertEqual(result["evaluator2"]["decision_code"], "candidate_verified")
        self.assertEqual(result["evaluator1"]["decision_code"], "promotion_authorized")
        self.assertFalse(result["promotion_performed"])
        self.assertFalse(result["main_tree_modified"])
        self.assertEqual(model.calls, 1)
        self.assertEqual((self.root / "src/sira/sample.py").read_text(encoding="utf-8"), before)
        self.assertFalse((self.root / LIVE_CHECK_PATH).exists())
        artifact = Path(result["artifact"])
        self.assertTrue(artifact.is_file())
        persisted = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "passed")

    def test_live_check_rejects_model_edit_outside_exact_diagnostic_path(self):
        from sira.writer_live_check import run_writer_live_check

        model = FakeCodeModel({
            "summary": "Wrong target.",
            "edits": [{
                "path": "src/sira/sample.py",
                "content": "VALUE = 2\n",
                "reason": "Should be rejected by live-check scope.",
            }],
        })
        writer = ResearchBackedCodeWriter(self.root, model)

        result = run_writer_live_check(self.root, writer=writer, runner=PassingRunner())

        self.assertEqual(result["status"], "rejected_writer_scope")
        self.assertFalse(result["promotion_performed"])
        self.assertFalse(result["main_tree_modified"])
        self.assertEqual((self.root / "src/sira/sample.py").read_text(encoding="utf-8"), "VALUE = 1\n")

    def test_cli_self_writer_check_prints_json(self):
        payload = {"status": "passed", "kind": "writer_live_check", "promotion_performed": False}
        output = StringIO()
        with patch("sira.cli.run_writer_live_check", return_value=payload), patch("sys.stdout", output):
            rc = main(["--root", str(self.root), "self", "writer-check"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue()), payload)


if __name__ == "__main__":
    unittest.main()

class WriterLiveCheckRetryAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src/sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "src/sira/sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "tests/test_sample.py").write_text("# sample\n", encoding="utf-8")
        (self.root / "benchmarks/improvement_cases.json").write_text("[]\n", encoding="utf-8")
        (self.root / "README.md").write_text("SIRA\n", encoding="utf-8")
        (self.root / ".env.example").write_text("GEMINI_API_KEY=\n", encoding="utf-8")
        (self.root / "sira.py").write_text("# entry\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_provider_retry_metadata_is_persisted_without_raw_error_data(self):
        from sira.models import ProviderError
        from sira.writer_live_check import run_writer_live_check

        class FailingWriter:
            last_report = None

            def propose_text_edits(self, context):
                error = ProviderError("http_503", True, request_count=3)
                error.retry_delays = (0.5, 1.0)
                raise error

        result = run_writer_live_check(self.root, writer=FailingWriter(), runner=PassingRunner())

        self.assertEqual(result["status"], "writer_error")
        self.assertEqual(result["error"]["code"], "http_503")
        self.assertEqual(result["error"]["api_requests"], 3)
        self.assertEqual(result["error"]["retry_delays"], [0.5, 1.0])
        self.assertFalse(result["main_tree_modified"])
        self.assertFalse(result["promotion_performed"])
