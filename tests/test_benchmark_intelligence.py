from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.auto_evaluator import (
    benchmark_evidence_for_target,
    project_code_digest,
)
from sira.storage import write_json


class BenchmarkIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src/sira").mkdir(parents=True)
        (self.root / "src/sira/retrieval.py").write_text(
            "def load_evidence_run(value):\n    return value\n", encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _report(self, name: str, *, failed: int, digest: str | None = None):
        write_json(
            self.root / "runs" / name / "benchmark.json",
            {
                "schema_version": 1,
                "kind": "synthetic_regression_not_live_quality",
                "suite_id": "sira-retrieval-fixture-v1",
                "passed": max(0, 3 - failed),
                "failed": failed,
                "code_sha256": digest or project_code_digest(self.root),
                "api_requests": 0,
            },
        )

    def test_one_off_current_failure_is_evidence_without_priority_boost(self):
        self._report("one", failed=1)
        row = benchmark_evidence_for_target(
            self.root, {"path": "src/sira/retrieval.py"}
        )
        self.assertEqual(row["classification"], "one_off_current_regression")
        self.assertEqual(row["priority_boost"], 0)
        self.assertFalse(row["authority_granted"])

    def test_two_current_failures_create_bounded_recurring_signal(self):
        self._report("one", failed=1)
        self._report("two", failed=2)
        row = benchmark_evidence_for_target(
            self.root, {"capability": "retrieval"}
        )
        self.assertEqual(row["classification"], "recurring_current_regression")
        self.assertGreater(row["priority_boost"], 0)
        self.assertLessEqual(row["priority_boost"], 160)
        self.assertEqual(row["history"]["current_code_failed_reports"], 2)

    def test_stale_failures_never_establish_recurrence(self):
        self._report("one", failed=3, digest="0" * 64)
        self._report("two", failed=3, digest="1" * 64)
        row = benchmark_evidence_for_target(
            self.root, {"capability": "retrieval"}
        )
        self.assertEqual(row["classification"], "stale_or_unknown_only")
        self.assertEqual(row["priority_boost"], 0)
        self.assertFalse(row["history"]["recurring_weakness"])

    def test_evidence_contains_no_raw_output_or_authority(self):
        self._report("one", failed=1)
        self._report("two", failed=1)
        row = benchmark_evidence_for_target(
            self.root, {"capability": "retrieval"}
        )
        rendered = json.dumps(row, sort_keys=True).casefold()
        self.assertNotIn("output_tail", rendered)
        self.assertNotIn("traceback", rendered)
        self.assertFalse(row["promotion_authorized"])
        self.assertFalse(row["external_access_requested"])


if __name__ == "__main__":
    unittest.main()
