"""A85: independent, bounded and provenance-bound evaluation."""
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.advanced_evaluation import (register_suite, evaluate_candidate,
    candidate_visible_context, load_suite, fresh_case_ids,
    recent_independent_evidence, isolated_python_runner)
from sira.engineering_evaluator import evaluate_engineering_candidate
from sira.self_model import collect_self_model
import tests.test_engineering_evaluator as engineering_fixture


class AdvancedEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "main"
        self.root.mkdir()
        self.baseline = Path(self.temp.name) / "baseline"
        self.candidate = Path(self.temp.name) / "candidate"
        for path in (self.baseline, self.candidate):
            (path / "src").mkdir(parents=True)
            (path / "src" / "app.py").write_text("def answer(value):\n    return value\n")
        self.suite = {
            "suite_id": "app_answer", "version": "1", "target_path": "src/app.py",
            "function": "answer", "cases": [
                {"id": "p1", "class": "public", "input": 1, "expected": 1},
                {"id": "h1", "class": "hidden", "input": 2, "expected": 2, "critical": True},
                {"id": "f1", "class": "fresh", "input": 3, "expected": 3},
                {"id": "f2", "class": "fresh", "input": 4, "expected": 4},
            ],
        }

    def register(self):
        return register_suite(self.root, self.suite)

    @staticmethod
    def runner(subject, target, function, value):
        namespace = {}
        exec((subject / target).read_text(), namespace)
        return {"status": "completed", "observed": namespace[function](value)}

    def evaluate(self, **kwargs):
        return evaluate_candidate(self.root, self.baseline, self.candidate,
                                  "app_answer", seed=37, runner=self.runner, **kwargs)

    def test_hidden_expected_not_in_candidate_context(self):
        self.register()
        context = candidate_visible_context(self.root, "app_answer")
        self.assertNotIn("expected", json.dumps(context))
        self.assertNotIn("h1", json.dumps(context))
        self.assertNotIn("f1", json.dumps(context))

    def test_public_overfit_rejected_by_hidden(self):
        self.register()
        (self.candidate / "src/app.py").write_text(
            "def answer(value):\n    return 999 if value == 2 else value\n")
        report = self.evaluate()
        self.assertEqual(report["verdict"], "reject")
        self.assertTrue(report["results"]["candidate"]["public"]["passed"])
        self.assertFalse(report["results"]["candidate"]["hidden"]["passed"])
        self.assertTrue(report["regressions"])

    def test_valid_candidate_independent_pass(self):
        self.register()
        report = self.evaluate()
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["evaluator"], "independent_a85_v1")
        self.assertFalse(report["promotion_authorized"])
        self.assertEqual(report["results"]["baseline"]["case_ids"],
                         report["results"]["candidate"]["case_ids"])
        self.assertEqual(report["results"]["candidate"]["cases"][0]["expected_result_sha256"],
                         report["results"]["candidate"]["cases"][0]["observed_sha256"])

    def test_seed_and_version_reproducible(self):
        self.register()
        suite = load_suite(self.root, "app_answer")
        self.assertEqual(fresh_case_ids(suite, 37), fresh_case_ids(suite, 37))
        self.assertEqual(self.evaluate()["seed"], 37)
        self.assertEqual(self.evaluate()["suite_version"], "1")

    def test_tampered_manifest_fails_closed(self):
        path = self.register()
        raw = json.loads(path.read_text())
        raw["cases"][1]["expected"] = 999
        path.write_text(json.dumps(raw))
        with self.assertRaises(ValueError):
            self.evaluate()

    def test_timeout_and_incomplete_cannot_pass(self):
        self.register()
        report = evaluate_candidate(self.root, self.baseline, self.candidate,
            "app_answer", seed=37, runner=lambda *args: {"status": "timeout"})
        self.assertEqual(report["verdict"], "inconclusive")

    def test_evidence_persisted_with_provenance(self):
        self.register()
        report = self.evaluate()
        path = self.root / "memory/advanced_evaluation/reports" / (report["evaluation_id"] + ".json")
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text())["suite_sha256"], report["suite_sha256"])
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(report["model_requests"], 0)

    def test_duplicate_or_malformed_suite_rejected(self):
        self.register()
        with self.assertRaises(ValueError):
            self.register()
        self.suite["cases"].append({"id": "p1", "class": "hidden", "input": 3, "expected": 3})
        with self.assertRaises(ValueError):
            register_suite(self.root, {**self.suite, "suite_id": "other"})

    def test_unavailable_is_inconclusive(self):
        self.register()
        report = evaluate_candidate(self.root, self.baseline, self.candidate,
                                    "app_answer", seed=37,
                                    runner=lambda *args: {"status": "unavailable"})
        self.assertEqual(report["verdict"], "inconclusive")
        self.assertFalse(report["promotion_authorized"])

    def test_report_does_not_grant_authority(self):
        self.register()
        report = self.evaluate()
        for key in ("authority_granted", "promotion_authorized", "promotion_performed",
                    "paid_spending_authorized", "skill_activated"):
            self.assertIs(report[key], False)

    def test_engineering_gate_rejects_failed_registered_suite(self):
        self.register()
        helper = engineering_fixture.EngineeringEvaluatorTests()
        # This fixture uses the same target path as the registered suite.
        base = Path(self.temp.name) / "engineering"
        base.mkdir()
        root, attempt = helper.make_attempt(base)
        register_suite(root, self.suite)
        with patch("sira.engineering_evaluator.evaluate_candidate",
                   return_value={"verdict": "reject", "evaluation_id": "ae_fixture",
                                 "suite_id": "app_answer", "suite_sha256": "a" * 64}):
            decision = evaluate_engineering_candidate(root, attempt)
        self.assertEqual(decision["decision"], "reject")
        self.assertFalse(decision["promotion_candidate_eligible"])
        self.assertFalse(decision["promotion_authorized"])

    def test_protected_evaluator_not_changed(self):
        from sira.self_modification import PROTECTED_PATHS
        self.assertIn("src/sira/evaluator1.py", PROTECTED_PATHS)

    def test_self_model_scopes_independent_evidence(self):
        self.register()
        report = self.evaluate()
        model = collect_self_model(self.root)
        row = model["capabilities"]["coding_engineering"]
        self.assertEqual(row["state"], "partially_demonstrated")
        self.assertIn("not_general", row["reason"])
        self.assertEqual(row["evidence"][0]["ref"], report["evaluation_id"] + ".json")
        self.assertFalse(model["authority_granted"])

    def test_tampered_report_not_used_by_self_model(self):
        self.register()
        report = self.evaluate()
        path = self.root / "memory/advanced_evaluation/reports" / (report["evaluation_id"] + ".json")
        value = json.loads(path.read_text())
        value["suite_version"] = "tampered"
        path.write_text(json.dumps(value))
        self.assertEqual(recent_independent_evidence(self.root), [])
        self.assertEqual(collect_self_model(self.root)["capabilities"]["coding_engineering"]["state"],
                         "unverified")

    def test_isolated_runner_fails_closed_without_namespace(self):
        with patch("sira.advanced_evaluation.shutil.which", return_value=None):
            outcome = isolated_python_runner(self.candidate, "src/app.py", "answer", 2)
        self.assertEqual(outcome["status"], "unavailable")

    def test_candidate_mount_excludes_hidden_and_main_memory(self):
        calls = []
        def blocked(argv, **kwargs):
            calls.append(argv)
            raise OSError("private namespace unavailable")
        with patch("sira.advanced_evaluation.shutil.which", return_value="/usr/bin/bwrap"), \
             patch("sira.advanced_evaluation.subprocess.run", side_effect=blocked):
            outcome = isolated_python_runner(self.candidate, "src/app.py", "answer", 2)
        self.assertEqual(outcome["status"], "unavailable")
        argv = calls[0]
        self.assertIn("--unshare-all", argv)
        self.assertIn("--clearenv", argv)
        self.assertNotIn(str(self.root), argv)
        self.assertNotIn("memory", " ".join(argv))

    def test_report_count_is_bounded(self):
        self.register()
        with patch("sira.advanced_evaluation.MAX_REPORTS", 1):
            self.evaluate()
            with self.assertRaises(ValueError):
                self.evaluate()
