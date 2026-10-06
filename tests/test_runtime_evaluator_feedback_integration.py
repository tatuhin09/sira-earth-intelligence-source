from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.autonomous_targeting import select_autonomous_target
from sira.models import utc_now
from sira.runtime import RuntimeStateStore, run_unified_improvement_cycle
from sira.runtime_evaluator_feedback import RuntimeEvaluatorFeedbackStore


class RuntimeEvaluatorFeedbackIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def memory(memory_id: str):
        return {
            "memory_id": memory_id,
            "kind": "failure",
            "status": "observed",
            "category": "provider",
            "capability": "retrieval",
            "provider": "real_provider",
            "occurrence_count": 1,
            "priority_score": 20,
            "last_seen_at": "2030-01-01T00:00:00+00:00",
        }

    @staticmethod
    def discovery():
        return {
            "schema_version": 1,
            "kind": "opportunity_discovery",
            "opportunities": [],
            "eligible_count": 0,
            "suppressed_cooldown_count": 0,
            "api_requests": 0,
            "paid_spending": False,
        }

    def test_target_selection_reuses_feedback_context_without_score_mutation(self):
        memory = self.memory("m_" + "1" * 32)
        target = {"target_kind": "memory", **memory}
        RuntimeEvaluatorFeedbackStore(self.root).record(
            {
                "cycle_id": "sc_" + "2" * 32,
                "status": "completed",
                "outcome": "validated",
                "promotion_performed": False,
                "main_tree_modified": False,
            },
            target,
        )
        result = select_autonomous_target(
            self.root,
            now_epoch=2_000_000_000,
            memory_candidates_fn=lambda _root, _limit: [memory],
            opportunity_discovery_fn=lambda _root, _limit, _now: self.discovery(),
        )
        selected = result["target"]
        self.assertEqual(selected["runtime_feedback"]["cycle_count"], 1)
        self.assertFalse(selected["runtime_feedback"]["used_for_ranking"])
        self.assertEqual(
            selected["effective_priority_score"],
            selected["raw_priority_score"] + selected["benchmark_priority_boost"],
        )
        self.assertEqual(result["runtime_feedback_reused_count"], 1)
        self.assertFalse(result["runtime_feedback_authoritative"])

    def test_unified_memory_cycle_persists_feedback_and_benchmark_snapshot(self):
        store = RuntimeStateStore(self.root)
        store.save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": os.getpid(),
            "started_at": utc_now(),
            "heartbeat_at": utc_now(),
            "generation": 1,
        })
        target = {
            "target_kind": "memory",
            **self.memory("m_" + "3" * 32),
            "benchmark_evidence": {
                "suite": "retrieval",
                "classification": "one_off_current_regression",
                "priority_boost": 0,
                "authority_granted": False,
                "promotion_authorized": False,
            },
            "runtime_feedback": {
                "cycle_count": 0,
                "history_applied": False,
                "used_for_ranking": False,
            },
        }
        selection = {
            "selection_id": "ats_" + "4" * 32,
            "status": "selected",
            "target": target,
            "alternatives": [],
            "selection_policy": "test",
        }

        def fake_cycle(_root, generation, **_kwargs):
            return {
                "schema_version": 1,
                "kind": "self_improvement_cycle",
                "cycle_id": "sc_" + "5" * 32,
                "generation": generation,
                "status": "completed",
                "outcome": "validated",
                "promotion_performed": False,
                "main_tree_modified": False,
            }

        result = run_unified_improvement_cycle(
            self.root,
            1,
            target_selector=lambda _root: selection,
            memory_cycle_runner=fake_cycle,
            keep_runtime_on=False,
        )
        self.assertEqual(
            result["benchmark_evidence"]["classification"],
            "one_off_current_regression",
        )
        self.assertEqual(result["evaluator_feedback"]["classification"], "completed_nonpromotion")
        self.assertTrue(Path(result["evaluator_feedback"]["artifact"]).is_file())
        summary = RuntimeEvaluatorFeedbackStore(self.root).summary(target)
        self.assertEqual(summary["cycle_count"], 1)


if __name__ == "__main__":
    unittest.main()
