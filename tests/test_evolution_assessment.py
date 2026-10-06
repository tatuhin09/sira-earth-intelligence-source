from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SRC_TEXT = str(SRC)
sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]

from sira.autonomous_targeting import opportunity_lineage_key
from sira.evolution_assessment import EvolutionGapStore, assess_evolution_attempt


class EvolutionAssessmentTests(unittest.TestCase):
    def test_promotion_is_measured_gain_and_consumes_strategy(self):
        result = assess_evolution_attempt({
            "status": "promoted",
            "outcome": "promotion_committed",
            "promotion_performed": True,
        })
        self.assertEqual(result["classification"], "promotion_gain")
        self.assertTrue(result["strategy_consumed"])
        self.assertTrue(result["measured_comparison_available"])

    def test_writer_infrastructure_failure_is_neutral(self):
        result = assess_evolution_attempt({
            "status": "writer_error",
            "outcome": "http_503",
            "promotion_performed": False,
            "error": {"raw_error": "do not retain me"},
        })
        self.assertEqual(result["classification"], "infrastructure_neutral")
        self.assertFalse(result["strategy_consumed"])
        self.assertNotIn("raw_error", repr(result))

    def test_structural_no_gain_is_consumed_with_bounded_metric_summary(self):
        result = assess_evolution_attempt({
            "status": "rejected_structural_goal",
            "outcome": "structural_goal_not_met",
            "promotion_performed": False,
            "structural_check": {
                "metric": "branch_points",
                "path": "src/sira/example.py",
                "symbol": "example",
                "baseline": 4,
                "candidate": 4,
                "decision": "fail",
                "decision_code": "structural_goal_not_met",
            },
        })
        self.assertEqual(result["classification"], "measured_no_gain")
        self.assertTrue(result["strategy_consumed"])
        self.assertEqual(result["structural"]["baseline"], 4)
        self.assertEqual(result["structural"]["candidate"], 4)

    def test_candidate_regression_keeps_only_aggregate_test_evidence(self):
        result = assess_evolution_attempt({
            "status": "rejected",
            "outcome": "candidate_regression",
            "promotion_performed": False,
            "baseline": {
                "overall_passed": True,
                "tests": {"passed": True, "test_count": 20, "output_tail": "secret raw"},
            },
            "candidate": {
                "overall_passed": False,
                "tests": {"passed": False, "test_count": 20, "output_tail": "traceback raw"},
            },
        })
        self.assertEqual(result["classification"], "candidate_rejected")
        self.assertTrue(result["strategy_consumed"])
        rendered = repr(result).casefold()
        self.assertNotIn("output_tail", rendered)
        self.assertNotIn("traceback raw", rendered)
        self.assertNotIn("secret raw", rendered)

    def test_strategy_exhaustion_gap_is_non_authoritative_and_bounded(self):
        opportunity = {
            "opportunity_id": "op_" + "1" * 32,
            "fingerprint": "2" * 64,
            "type": "complex_function",
            "path": "src/sira/example.py",
            "symbol": "example",
            "source_sha256": "3" * 64,
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = EvolutionGapStore(Path(tmp))
            gap = store.record_strategy_exhaustion(
                opportunity,
                {
                    "status": "strategy_exhausted",
                    "attempted_strategy_count": 3,
                    "candidate_strategy_count": 3,
                },
            )
            loaded = store.load(opportunity_lineage_key(opportunity))

        self.assertEqual(gap["gap_kind"], "strategy_exhausted")
        self.assertFalse(gap["authority_granted"])
        self.assertFalse(gap["external_access_requested"])
        self.assertFalse(gap["promotion_authorized"])
        self.assertEqual(loaded["lineage_key"], gap["lineage_key"])


if __name__ == "__main__":
    unittest.main()
