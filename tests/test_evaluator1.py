from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class Evaluator1Tests(unittest.TestCase):
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
        (self.root / "src" / "sira" / "evaluator1.py").write_text("GATE = True\n", encoding="utf-8")
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
    def health(test_count=145, passed_cases=4):
        return {
            "tests": {"passed": True, "test_count": test_count, "returncode": 0},
            "benchmark": {
                "passed": True,
                "passed_cases": passed_cases,
                "failed_cases": 0,
                "returncode": 0,
            },
            "overall_passed": True,
        }

    def accepted_evaluator2(self):
        from sira.evaluator2 import evaluate_candidate

        return evaluate_candidate(
            self.root,
            self.candidate,
            self.health(),
            self.health(),
        )

    @staticmethod
    def forged_accept():
        return {
            "schema_version": 1,
            "kind": "evaluator2_report",
            "policy_version": 1,
            "decision": "accept",
            "decision_code": "candidate_verified",
            "promotion_recommended": True,
            "risk_flags": [],
            "checks": {
                "baseline_healthy": True,
                "candidate_healthy": True,
                "paths_allowed": True,
                "protected_unchanged": True,
                "text_only": True,
                "symlink_free": True,
                "bounded_diff": True,
            },
            "diff": {
                "changed_files": ["src/sira/feature.py"],
                "changed_file_count": 1,
            },
        }

    def test_allows_verified_candidate_and_binds_authorization_to_current_content(self):
        from sira.evaluator1 import evaluate_final_gate
        from sira.evaluator2 import evaluate_candidate

        (self.candidate / "src" / "sira" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        evaluator2 = evaluate_candidate(self.root, self.candidate, self.health(), self.health())
        before = (self.root / "src" / "sira" / "feature.py").read_text(encoding="utf-8")
        report = evaluate_final_gate(
            self.root,
            self.candidate,
            evaluator2,
            self.health(),
            self.health(),
        )

        self.assertEqual(report["decision"], "allow")
        self.assertEqual(report["decision_code"], "promotion_authorized")
        self.assertTrue(report["promotion_allowed"])
        self.assertFalse(report["promotion_performed"])
        self.assertEqual(len(report["authorization"]["candidate_public_sha256"]), 64)
        self.assertEqual(len(report["authorization"]["main_public_sha256"]), 64)
        self.assertEqual(len(report["authorization"]["protected_shell_sha256"]), 64)
        self.assertEqual(len(report["authorization"]["evaluator2_report_sha256"]), 64)
        self.assertEqual(len(report["authorization"]["fingerprint"]), 64)
        self.assertEqual((self.root / "src" / "sira" / "feature.py").read_text(encoding="utf-8"), before)

    def test_denies_when_evaluator2_did_not_recommend_real_change(self):
        from sira.evaluator1 import evaluate_final_gate

        report = evaluate_final_gate(
            self.root,
            self.candidate,
            self.accepted_evaluator2(),
            self.health(),
            self.health(),
        )
        self.assertEqual(report["decision"], "deny")
        self.assertEqual(report["decision_code"], "evaluator2_not_authorized")
        self.assertFalse(report["promotion_allowed"])

    def test_denies_candidate_when_main_changed_after_workspace_was_created(self):
        from sira.evaluator1 import evaluate_final_gate

        (self.root / "src" / "sira" / "feature.py").write_text("VALUE = 99\n", encoding="utf-8")
        report = evaluate_final_gate(
            self.root,
            self.candidate,
            self.forged_accept(),
            self.health(),
            self.health(),
        )
        self.assertEqual(report["decision"], "deny")
        self.assertEqual(report["decision_code"], "stale_candidate_base")

    def test_independently_denies_protected_shell_tamper_even_with_forged_upstream_accept(self):
        from sira.evaluator1 import evaluate_final_gate

        (self.candidate / "src" / "sira" / "runtime.py").write_text("PROTECTED = False\n", encoding="utf-8")
        report = evaluate_final_gate(
            self.root,
            self.candidate,
            self.forged_accept(),
            self.health(),
            self.health(),
        )
        self.assertEqual(report["decision"], "deny")
        self.assertEqual(report["decision_code"], "protected_shell_mismatch")
        self.assertFalse(report["checks"]["protected_shell_unchanged"])

    def test_independently_denies_public_symlink_even_with_forged_upstream_accept(self):
        from sira.evaluator1 import evaluate_final_gate

        (self.candidate / "src" / "sira" / "link.py").symlink_to(self.root / "README.md")
        report = evaluate_final_gate(
            self.root,
            self.candidate,
            self.forged_accept(),
            self.health(),
            self.health(),
        )
        self.assertEqual(report["decision"], "deny")
        self.assertEqual(report["decision_code"], "candidate_integrity_failed")
        self.assertFalse(report["checks"]["symlink_free"])

    def test_denies_verification_shrinkage_even_when_both_health_objects_pass(self):
        from sira.evaluator1 import evaluate_final_gate

        (self.candidate / "src" / "sira" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        report = evaluate_final_gate(
            self.root,
            self.candidate,
            self.forged_accept(),
            self.health(test_count=145, passed_cases=4),
            self.health(test_count=144, passed_cases=3),
        )
        self.assertEqual(report["decision"], "deny")
        self.assertEqual(report["decision_code"], "verification_regression")
        self.assertFalse(report["checks"]["verification_not_weaker"])


if __name__ == "__main__":
    unittest.main()
