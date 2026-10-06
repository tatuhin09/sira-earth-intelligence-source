"""Regression for the retrieve refactor: preserve feedback and reduce branches."""

from contextlib import closing
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.autonomous_promotion import _branch_points_for_symbol, _structural_goal_check
from sira.memory import MemoryStore, Observation


class MemoryRetrievalFeedbackTests(unittest.TestCase):
    def test_feedback_weights_and_visible_score_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory))
            memory_id, _ = store.upsert(
                Observation(
                    "success", "success", "retrieval",
                    "provider retrieval strategy", "validated",
                    signature="retrieval-feedback-v067",
                ),
                run_id="67" * 16,
                artifact_name="result.json",
                artifact_sha256="6" * 64,
                outcome_status="completed",
            )
            for kind, weight in (
                ("retrieval_helped", 2.5),
                ("application_succeeded", 2.0),
                ("retrieval_irrelevant", 1.0),
                ("application_failed", 0.25),
                ("retrieval_used", 0.75),
            ):
                store.record_outcome(memory_id, kind, context={}, weight=weight)

            with closing(store._connect()) as conn:
                self.assertEqual(
                    store._retrieval_feedback_weights(conn, memory_id),
                    (4.5, 1.25, 0.75),
                )

            row = next(item for item in store.retrieve("provider retrieval", 5)
                       if item["memory_id"] == memory_id)
            feedback = next(reason for reason in row["score_reasons"]
                            if reason["component"] == "outcome_feedback")
            self.assertEqual(feedback, {
                "component": "outcome_feedback",
                "effect": "boost",
                "value": 0.0875,
                "retrieval_helped_weight": 4.5,
                "retrieval_irrelevant_weight": 1.25,
                "retrieval_used_weight": 0.75,
            })

    def test_retrieve_meets_existing_branch_reduction_goal(self):
        branches = _branch_points_for_symbol(ROOT, "src/sira/memory.py", "retrieve")
        result = _structural_goal_check(ROOT, ROOT, {
            "target": {"path": "src/sira/memory.py", "symbol": "retrieve"},
            "success_criteria": {"structural_goal": {
                "metric": "branch_points",
                "baseline": branches,
                "target_max": 33,
            }},
        })
        self.assertLessEqual(result["candidate"], 33)
        self.assertEqual(result["decision"], "pass")


if __name__ == "__main__":
    unittest.main()
