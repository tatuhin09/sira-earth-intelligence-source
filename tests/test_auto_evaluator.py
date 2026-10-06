from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.auto_evaluator import (
    benchmark_history_summary,
    compare_health,
    project_code_digest,
    select_benchmark_suite,
)
from sira.storage import write_json


def health(tests=10, cases=4, failed=0, overall=True):
    return {
        "overall_passed": overall,
        "tests": {"passed": overall, "test_count": tests, "output_tail": "raw"},
        "benchmark": {
            "passed": overall and failed == 0,
            "passed_cases": cases,
            "failed_cases": failed,
            "output_tail": "traceback raw",
        },
    }


class AutoEvaluatorTests(unittest.TestCase):
    def test_explicit_supported_suite_is_preserved(self):
        row = select_benchmark_suite({"benchmark_suite": "papers"})
        self.assertEqual(row["suite"], "papers")
        self.assertEqual(row["selection_source"], "explicit")
        self.assertFalse(row["authority_granted"])

    def test_unsupported_explicit_suite_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            select_benchmark_suite({"benchmark_suite": "invented-suite"})

    def test_path_and_capability_fallbacks_are_deterministic(self):
        self.assertEqual(
            select_benchmark_suite({"path": "src/sira/memory.py"})["suite"],
            "memory",
        )
        self.assertEqual(
            select_benchmark_suite({"capability": "paper_reading"})["suite"],
            "paper-reading",
        )
        self.assertIsNone(select_benchmark_suite({"path": "docs/notes.md"})["suite"])

    def test_regression_shrinkage_equivalent_and_expanded_are_distinct(self):
        self.assertEqual(
            compare_health(health(), health(overall=False, failed=1, cases=0))["classification"],
            "candidate_regression",
        )
        self.assertEqual(
            compare_health(health(), health(tests=9))["classification"],
            "verification_shrinkage",
        )
        self.assertEqual(
            compare_health(health(), health())["classification"],
            "verification_equivalent",
        )
        self.assertEqual(
            compare_health(health(), health(tests=11, cases=5))["classification"],
            "verification_expanded",
        )

    def test_comparison_is_aggregate_only(self):
        rendered = repr(compare_health(health(), health())).casefold()
        self.assertNotIn("output_tail", rendered)
        self.assertNotIn("traceback raw", rendered)

    def test_recurring_history_requires_two_current_code_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = project_code_digest(root)
            for i in range(2):
                write_json(
                    root / "runs" / f"{i:032x}" / "memory-benchmark.json",
                    {
                        "suite_id": "sira-memory-quality-v1",
                        "kind": "memory_benchmark",
                        "code_sha256": current,
                        "passed": 23,
                        "failed": 1,
                    },
                )
            write_json(
                root / "runs" / ("f" * 32) / "memory-benchmark.json",
                {
                    "suite_id": "sira-memory-quality-v1",
                    "kind": "memory_benchmark",
                    "code_sha256": "0" * 64,
                    "passed": 0,
                    "failed": 99,
                },
            )
            summary = benchmark_history_summary(root, "memory")
        self.assertTrue(summary["recurring_weakness"])
        self.assertEqual(summary["current_code_failed_reports"], 2)
        self.assertEqual(summary["stale_or_unknown_reports"], 1)


if __name__ == "__main__":
    unittest.main()
