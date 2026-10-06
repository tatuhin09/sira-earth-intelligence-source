from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class Evaluator2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "main"
        (self.root / "src" / "sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "src" / "sira" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "src" / "sira" / "runtime.py").write_text("PROTECTED = True\n", encoding="utf-8")
        (self.root / "src" / "sira" / "cli.py").write_text("CLI = True\n", encoding="utf-8")
        (self.root / "src" / "sira" / "self_modification.py").write_text("BOUNDARY = True\n", encoding="utf-8")
        (self.root / "tests" / "test_feature.py").write_text("# test\n", encoding="utf-8")
        (self.root / "benchmarks" / "case.json").write_text("{}\n", encoding="utf-8")
        (self.root / "docs" / "note.md").write_text("note\n", encoding="utf-8")
        (self.root / "sira.py").write_text("print('sira')\n", encoding="utf-8")
        (self.root / "README.md").write_text("readme\n", encoding="utf-8")
        (self.root / ".env.example").write_text("SAFE=1\n", encoding="utf-8")

        from sira.self_modification import prepare_candidate_workspace

        self.candidate = Path(self.tmp.name) / "candidate"
        prepare_candidate_workspace(self.root, self.candidate)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def healthy():
        return {
            "tests": {"passed": True, "test_count": 10, "returncode": 0},
            "benchmark": {"passed": True, "passed_cases": 4, "failed_cases": 0, "returncode": 0},
            "overall_passed": True,
        }

    def test_accepts_bounded_allowed_text_diff_when_candidate_checks_pass(self):
        from sira.evaluator2 import evaluate_candidate

        (self.candidate / "src" / "sira" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        report = evaluate_candidate(self.root, self.candidate, self.healthy(), self.healthy())

        self.assertEqual(report["decision"], "accept")
        self.assertEqual(report["decision_code"], "candidate_verified")
        self.assertEqual(report["diff"]["modified_files"], ["src/sira/feature.py"])
        self.assertEqual(report["diff"]["changed_file_count"], 1)
        self.assertEqual(report["risk_flags"], [])
        self.assertTrue(report["checks"]["paths_allowed"])
        self.assertTrue(report["checks"]["protected_unchanged"])

    def test_rejects_protected_path_change_even_when_tests_pass(self):
        from sira.evaluator2 import evaluate_candidate

        (self.candidate / "src" / "sira" / "runtime.py").write_text("PROTECTED = False\n", encoding="utf-8")
        report = evaluate_candidate(self.root, self.candidate, self.healthy(), self.healthy())

        self.assertEqual(report["decision"], "reject")
        self.assertEqual(report["decision_code"], "structural_risk")
        self.assertIn("protected_path_changed", report["risk_flags"])
        self.assertFalse(report["checks"]["protected_unchanged"])

    def test_rejects_unapproved_removed_symlink_and_binary_changes(self):
        from sira.evaluator2 import evaluate_candidate

        (self.candidate / "README.md").write_text("changed outside modifiable roots\n", encoding="utf-8")
        (self.candidate / "docs" / "note.md").unlink()
        (self.candidate / "src" / "sira" / "link.py").symlink_to(self.root / "README.md")
        (self.candidate / "src" / "sira" / "binary.py").write_bytes(b"\xff\xfe\x00")
        report = evaluate_candidate(self.root, self.candidate, self.healthy(), self.healthy())

        self.assertEqual(report["decision"], "reject")
        self.assertIn("unapproved_path_changed", report["risk_flags"])
        self.assertIn("file_removed", report["risk_flags"])
        self.assertIn("symlink_present", report["risk_flags"])
        self.assertIn("non_utf8_text", report["risk_flags"])
        self.assertIn("docs/note.md", report["diff"]["removed_files"])

    def test_candidate_regression_is_rejected_and_unhealthy_baseline_is_blocked(self):
        from sira.evaluator2 import evaluate_candidate

        bad_candidate = self.healthy()
        bad_candidate = {**bad_candidate, "overall_passed": False,
                         "tests": {"passed": False, "test_count": 10, "returncode": 1}}
        rejected = evaluate_candidate(self.root, self.candidate, self.healthy(), bad_candidate)
        self.assertEqual(rejected["decision"], "reject")
        self.assertEqual(rejected["decision_code"], "candidate_regression")

        bad_baseline = self.healthy()
        bad_baseline = {**bad_baseline, "overall_passed": False,
                        "tests": {"passed": False, "test_count": 10, "returncode": 1}}
        blocked = evaluate_candidate(self.root, self.candidate, bad_baseline, self.healthy())
        self.assertEqual(blocked["decision"], "block")
        self.assertEqual(blocked["decision_code"], "unhealthy_baseline")

    def test_unchanged_candidate_is_a_verified_no_change_not_a_false_rejection(self):
        from sira.evaluator2 import evaluate_candidate

        report = evaluate_candidate(self.root, self.candidate, self.healthy(), self.healthy())
        self.assertEqual(report["decision"], "accept")
        self.assertEqual(report["decision_code"], "no_change_verified")
        self.assertEqual(report["diff"]["changed_file_count"], 0)
        self.assertFalse(report["promotion_recommended"])


if __name__ == "__main__":
    unittest.main()
