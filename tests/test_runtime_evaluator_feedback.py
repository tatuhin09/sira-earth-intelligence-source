from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.runtime_evaluator_feedback import (
    RuntimeEvaluatorFeedbackStore,
    runtime_evaluator_feedback_summary,
    stable_runtime_target_key,
)


class RuntimeEvaluatorFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def opportunity(fingerprint: str):
        return {
            "target_kind": "opportunity",
            "opportunity_id": "op_" + fingerprint[:32],
            "fingerprint": fingerprint,
            "type": "complex_function",
            "path": "src/sira/retrieval.py",
            "symbol": "load_evidence_run",
            "benchmark_evidence": {
                "suite": "retrieval",
                "classification": "recurring_current_regression",
                "priority_boost": 120,
                "history": {
                    "current_code_reports": 2,
                    "current_code_failed_reports": 2,
                    "current_code_passed_reports": 0,
                    "stale_or_unknown_reports": 0,
                    "recurring_weakness": True,
                },
            },
        }

    def test_opportunity_feedback_key_survives_source_fingerprint_change(self):
        self.assertEqual(
            stable_runtime_target_key(self.opportunity("1" * 64)),
            stable_runtime_target_key(self.opportunity("2" * 64)),
        )

    def test_memory_feedback_identity_is_per_memory(self):
        left = {"target_kind": "memory", "memory_id": "m_" + "1" * 32}
        right = {"target_kind": "memory", "memory_id": "m_" + "2" * 32}
        self.assertNotEqual(stable_runtime_target_key(left), stable_runtime_target_key(right))

    def test_explicit_and_raw_memory_shapes_share_identity(self):
        explicit = {"target_kind": "memory", "memory_id": "m_" + "a" * 32}
        raw = {"memory_id": "m_" + "a" * 32, "category": "provider"}
        self.assertEqual(
            stable_runtime_target_key(explicit),
            stable_runtime_target_key(raw),
        )

    def test_explicit_and_raw_opportunity_shapes_share_identity(self):
        explicit = {
            "target_kind": "opportunity",
            "type": "complex_function",
            "path": "src/sira/retrieval.py",
            "symbol": "load_evidence_run",
        }
        raw = {
            "type": "complex_function",
            "path": "src/sira/retrieval.py",
            "symbol": "load_evidence_run",
            "fingerprint": "f" * 64,
        }
        self.assertEqual(
            stable_runtime_target_key(explicit),
            stable_runtime_target_key(raw),
        )

    def test_record_is_aggregate_only_and_non_authoritative(self):
        report = RuntimeEvaluatorFeedbackStore(self.root).record(
            {
                "cycle_id": "sc_" + "3" * 32,
                "status": "failed",
                "outcome": "cycle_error",
                "promotion_performed": False,
                "main_tree_modified": False,
                "error": {"message": "secret traceback raw"},
                "worker_coordination": {"raw_error": "do not retain"},
            },
            self.opportunity("4" * 64),
        )
        rendered = json.dumps(report, sort_keys=True).casefold()
        self.assertNotIn("secret traceback raw", rendered)
        self.assertNotIn("raw_error", rendered)
        self.assertFalse(report["authority_granted"])
        self.assertFalse(report["promotion_authorized"])
        self.assertFalse(report["used_for_ranking"])
        self.assertTrue(Path(report["artifact"]).is_file())

    def test_summary_aggregates_prior_cycles_without_ranking_effect(self):
        store = RuntimeEvaluatorFeedbackStore(self.root)
        target = self.opportunity("5" * 64)
        store.record(
            {
                "cycle_id": "sc_" + "6" * 32,
                "status": "completed",
                "outcome": "promotion_committed",
                "promotion_performed": True,
                "main_tree_modified": True,
            },
            target,
        )
        store.record(
            {
                "cycle_id": "sc_" + "7" * 32,
                "status": "completed",
                "outcome": "structural_goal_not_met",
                "promotion_performed": False,
                "main_tree_modified": False,
            },
            self.opportunity("8" * 64),
        )
        summary = store.summary(target)
        self.assertEqual(summary["cycle_count"], 2)
        self.assertEqual(summary["promotion_count"], 1)
        self.assertTrue(summary["history_applied"])
        self.assertFalse(summary["used_for_ranking"])

    def test_no_history_returns_safe_empty_context(self):
        summary = runtime_evaluator_feedback_summary(
            self.root,
            {"target_kind": "memory", "memory_id": "m_" + "9" * 32},
        )
        self.assertEqual(summary["cycle_count"], 0)
        self.assertFalse(summary["history_applied"])
        self.assertFalse(summary["authority_granted"])


if __name__ == "__main__":
    unittest.main()
