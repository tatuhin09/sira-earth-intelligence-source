from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _SequencedRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def evaluate(self, root, suite):
        self.calls.append((Path(root).resolve(), suite))
        if not self.responses:
            raise AssertionError("unexpected runner call")
        return self.responses.pop(0)


class AutonomousPromotionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "main"
        (self.root / "src" / "sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "src" / "sira" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
        for name in (
            "runtime.py", "cli.py", "self_modification.py", "evaluator1.py",
            "promotion.py", "rollback.py", "autonomous_promotion.py",
        ):
            (self.root / "src" / "sira" / name).write_text(f"PROTECTED = {name!r}\n", encoding="utf-8")
        (self.root / "tests" / "test_feature.py").write_text("# test\n", encoding="utf-8")
        (self.root / "benchmarks" / "case.json").write_text("{}\n", encoding="utf-8")
        (self.root / "docs" / "note.md").write_text("note\n", encoding="utf-8")
        (self.root / "sira.py").write_text("print('sira')\n", encoding="utf-8")
        (self.root / "README.md").write_text("readme\n", encoding="utf-8")
        (self.root / ".env.example").write_text("SAFE=1\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def health(*, tests=155, cases=4, passed=True):
        return {
            "tests": {"passed": passed, "test_count": tests, "returncode": 0 if passed else 1},
            "benchmark": {
                "passed": passed,
                "passed_cases": cases if passed else 0,
                "failed_cases": 0 if passed else 1,
                "returncode": 0 if passed else 1,
            },
            "overall_passed": passed,
        }

    def hypothesis(self):
        return {
            "hypothesis_id": "ih_" + "a" * 32,
            "memory_id": "m_" + "b" * 32,
            "benchmark_suite": "papers",
            "candidate_text_edits": {"src/sira/feature.py": "VALUE = 2\n"},
        }

    def test_structured_candidate_edit_passes_both_gates_and_promotes(self):
        from sira.autonomous_promotion import run_autonomous_candidate_promotion

        healthy = self.health()
        runner = _SequencedRunner([healthy, self.health(tests=156), self.health(tests=156)])
        result = run_autonomous_candidate_promotion(self.root, self.hypothesis(), runner=runner)

        self.assertEqual(result["status"], "promoted")
        self.assertEqual(result["outcome"], "promotion_committed")
        self.assertTrue(result["promotion_performed"])
        self.assertEqual(result["evaluator2"]["decision_code"], "candidate_verified")
        self.assertEqual(result["evaluator1"]["decision_code"], "promotion_authorized")
        self.assertEqual(result["promotion"]["status"], "promoted")
        self.assertEqual((self.root / "src" / "sira" / "feature.py").read_text(), "VALUE = 2\n")
        self.assertEqual(len(runner.calls), 3)

    def test_failed_post_promotion_verification_rolls_back_main_tree(self):
        from sira.autonomous_promotion import run_autonomous_candidate_promotion

        runner = _SequencedRunner([
            self.health(),
            self.health(tests=156),
            self.health(passed=False),
        ])
        result = run_autonomous_candidate_promotion(self.root, self.hypothesis(), runner=runner)

        self.assertEqual(result["status"], "rolled_back")
        self.assertFalse(result["promotion_performed"])
        self.assertTrue(result["promotion"]["rollback_verified"])
        self.assertEqual((self.root / "src" / "sira" / "feature.py").read_text(), "VALUE = 1\n")

    def test_missing_structured_edit_is_non_promoting_not_an_error(self):
        from sira.autonomous_promotion import run_autonomous_candidate_promotion

        hypothesis = dict(self.hypothesis())
        hypothesis.pop("candidate_text_edits")
        runner = _SequencedRunner([])
        result = run_autonomous_candidate_promotion(self.root, hypothesis, runner=runner)

        self.assertEqual(result["status"], "no_edit_proposed")
        self.assertFalse(result["promotion_performed"])
        self.assertEqual(runner.calls, [])
        self.assertEqual((self.root / "src" / "sira" / "feature.py").read_text(), "VALUE = 1\n")


class AutonomousCodeWriterAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "main"
        (self.root / "src" / "sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "src" / "sira" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
        for name in (
            "runtime.py", "cli.py", "self_modification.py", "evaluator1.py",
            "promotion.py", "rollback.py", "autonomous_promotion.py",
        ):
            (self.root / "src" / "sira" / name).write_text(f"PROTECTED = {name!r}\n", encoding="utf-8")
        (self.root / "tests" / "test_feature.py").write_text("# test\n", encoding="utf-8")
        (self.root / "benchmarks" / "case.json").write_text("{}\n", encoding="utf-8")
        (self.root / "docs" / "note.md").write_text("note\n", encoding="utf-8")
        (self.root / "sira.py").write_text("print('sira')\n", encoding="utf-8")
        (self.root / "README.md").write_text("readme\n", encoding="utf-8")
        (self.root / ".env.example").write_text("SAFE=1\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_writer_report_is_bound_into_autonomous_attempt_audit(self):
        from sira.autonomous_promotion import run_autonomous_candidate_promotion

        class Writer:
            last_report = {
                "model": {"provider": "fixture_code_model", "id": "fixture-v1"},
                "api_requests": 1,
                "input_tokens": 50,
                "output_tokens": 20,
                "context_sha256": "a" * 64,
            }

            def propose_text_edits(self, _context):
                return {}

        result = run_autonomous_candidate_promotion(
            self.root,
            {"hypothesis_id": "ih_" + "1" * 32, "memory_id": "m_" + "2" * 32},
            writer=Writer(),
        )
        self.assertEqual(result["status"], "no_edit_proposed")
        self.assertEqual(result["writer_report"]["api_requests"], 1)
        self.assertEqual(result["writer_report"]["model"]["provider"], "fixture_code_model")


class StructuralGoalGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "main"
        (self.root / "src" / "sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "src" / "sira" / "feature.py").write_text(
            "def target(x):\n    if x > 0:\n        if x > 1:\n            return 2\n        return 1\n    return 0\n",
            encoding="utf-8",
        )
        for name in (
            "runtime.py", "cli.py", "self_modification.py", "evaluator1.py",
            "promotion.py", "rollback.py", "autonomous_promotion.py",
        ):
            (self.root / "src" / "sira" / name).write_text(f"PROTECTED = {name!r}\n", encoding="utf-8")
        (self.root / "tests" / "test_feature.py").write_text("# test\n", encoding="utf-8")
        (self.root / "benchmarks" / "case.json").write_text("{}\n", encoding="utf-8")
        (self.root / "docs" / "note.md").write_text("note\n", encoding="utf-8")
        (self.root / "sira.py").write_text("print('sira')\n", encoding="utf-8")
        (self.root / "README.md").write_text("readme\n", encoding="utf-8")
        (self.root / ".env.example").write_text("SAFE=1\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_structural_goal_is_a_veto_before_expensive_verification(self):
        from sira.autonomous_promotion import run_autonomous_candidate_promotion

        hypothesis = {
            "benchmark_suite": "papers",
            "target": {"path": "src/sira/feature.py", "symbol": "target"},
            "success_criteria": {"structural_goal": {"metric": "branch_points", "baseline": 2, "target_max": 1}},
            "candidate_text_edits": {
                "src/sira/feature.py": "def target(x):\n    if x > 0:\n        if x > 1:\n            return 2\n        return 1\n    return 0\n"
            },
        }
        runner = _SequencedRunner([])
        result = run_autonomous_candidate_promotion(self.root, hypothesis, runner=runner)
        self.assertEqual(result["status"], "rejected_structural_goal")
        self.assertEqual(result["structural_check"]["candidate"], 2)
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
