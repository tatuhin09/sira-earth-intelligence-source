from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.evaluator2 import evaluate_candidate


def health(tests=10, cases=4, failed=0, overall=True):
    return {
        "overall_passed": overall,
        "tests": {"passed": overall, "test_count": tests},
        "benchmark": {
            "passed": overall and failed == 0,
            "passed_cases": cases,
            "failed_cases": failed,
        },
    }


class Evaluator2AutoEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.main = Path(self.tmp.name) / "main"
        self.candidate = Path(self.tmp.name) / "candidate"
        for root in (self.main, self.candidate):
            (root / "src/sira").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "benchmarks").mkdir()
            (root / "docs").mkdir()
            (root / "sira.py").write_text("print('x')\n", encoding="utf-8")
            (root / "src/sira/feature.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.candidate / "src/sira/feature.py").write_text("VALUE = 2\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_verification_shrinkage_is_rejected_before_protected_gate(self):
        report = evaluate_candidate(
            self.main, self.candidate, health(tests=10), health(tests=9)
        )
        self.assertEqual(report["decision"], "reject")
        self.assertEqual(report["decision_code"], "verification_shrinkage")
        self.assertFalse(report["promotion_recommended"])
        self.assertEqual(report["evaluation_classification"], "verification_shrinkage")

    def test_equal_verification_preserves_candidate_verified_contract(self):
        report = evaluate_candidate(self.main, self.candidate, health(), health())
        self.assertEqual(report["decision_code"], "candidate_verified")
        self.assertTrue(report["promotion_recommended"])
        self.assertEqual(report["evaluation_classification"], "verification_equivalent")

    def test_expanded_verification_is_audited_but_does_not_self_authorize(self):
        report = evaluate_candidate(
            self.main, self.candidate, health(), health(tests=11, cases=5)
        )
        self.assertEqual(report["decision_code"], "candidate_verified")
        self.assertEqual(report["evaluation_classification"], "verification_expanded")
        self.assertFalse(report["verification"]["promotion_authorized"])


if __name__ == "__main__":
    unittest.main()
