from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

# SIRA_TEST_SRC_PATH_FIX_V1
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SRC_TEXT = str(SRC)
sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]


class EvolutionStrategyPlannerContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _opportunity():
        return {
            "opportunity_id": "op_" + "1" * 32,
            "fingerprint": "2" * 64,
            "type": "complex_function",
            "path": "src/sira/example.py",
            "symbol": "example",
            "source_sha256": "3" * 64,
        }

    @staticmethod
    def _strategies():
        return [
            {
                "strategy": "Extract cohesive phases into private helpers while preserving behavior.",
                "basis": "local_evidence_plus_public_research",
                "citation_ids": ["R1"],
            },
            {
                "strategy": "Separate validation from execution while keeping the public contract stable.",
                "basis": "local_evidence_plus_public_research",
                "citation_ids": ["R1", "R2"],
            },
            {
                "strategy": "Use characterization tests before simplifying branch-heavy control flow.",
                "basis": "local_evidence_plus_public_research",
                "citation_ids": ["R2"],
            },
        ]

    def _planner(self):
        from sira.evolution import StrategyEvolutionPlanner
        return StrategyEvolutionPlanner(self.root)

    def test_no_history_selects_first_strategy_deterministically(self):
        plan = self._planner().plan(self._opportunity(), self._strategies())
        self.assertEqual(plan["schema"], "sira.evolution_strategy_plan.v1")
        self.assertEqual(plan["status"], "selected")
        self.assertEqual(plan["selected_index"], 0)
        self.assertEqual(
            plan["selected_strategy"]["strategy"],
            self._strategies()[0]["strategy"],
        )
        self.assertFalse(plan["history_applied"])
        self.assertEqual(plan["attempted_strategy_count"], 0)

    def test_rejected_strategy_is_not_repeated_and_next_distinct_strategy_is_selected(self):
        planner = self._planner()
        opportunity = self._opportunity()
        strategies = self._strategies()

        first = planner.plan(opportunity, strategies)
        planner.record_outcome(
            opportunity,
            first["selected_strategy"],
            outcome="rejected_structural_goal",
            promotion_performed=False,
        )

        second = planner.plan(opportunity, strategies)
        self.assertEqual(second["status"], "selected")
        self.assertEqual(second["selected_index"], 1)
        self.assertNotEqual(
            second["selected_strategy_fingerprint"],
            first["selected_strategy_fingerprint"],
        )
        self.assertTrue(second["history_applied"])
        self.assertEqual(second["attempted_strategy_count"], 1)

    def test_duplicate_strategy_text_is_deduplicated_before_selection(self):
        strategies = self._strategies()
        strategies.insert(1, dict(strategies[0]))
        plan = self._planner().plan(self._opportunity(), strategies)
        self.assertEqual(plan["candidate_strategy_count"], 3)
        self.assertEqual(plan["deduplicated_strategy_count"], 1)

    def test_exhausted_rejected_strategies_fail_closed_without_retry_loop(self):
        planner = self._planner()
        opportunity = self._opportunity()
        strategies = self._strategies()

        for _ in range(3):
            plan = planner.plan(opportunity, strategies)
            self.assertEqual(plan["status"], "selected")
            planner.record_outcome(
                opportunity,
                plan["selected_strategy"],
                outcome="rejected_evaluator2",
                promotion_performed=False,
            )

        exhausted = planner.plan(opportunity, strategies)
        self.assertEqual(exhausted["status"], "strategy_exhausted")
        self.assertIsNone(exhausted["selected_strategy"])
        self.assertEqual(exhausted["attempted_strategy_count"], 3)
        self.assertTrue(exhausted["history_applied"])

    def test_successful_strategy_suppresses_same_lineage_re_evolution(self):
        planner = self._planner()
        opportunity = self._opportunity()
        first = planner.plan(opportunity, self._strategies())
        planner.record_outcome(
            opportunity,
            first["selected_strategy"],
            outcome="promotion_committed",
            promotion_performed=True,
        )

        next_plan = planner.plan(opportunity, self._strategies())
        self.assertEqual(next_plan["status"], "recent_success_suppressed")
        self.assertIsNone(next_plan["selected_strategy"])
        self.assertTrue(next_plan["history_applied"])

    def test_outcome_history_is_aggregate_and_contains_no_raw_writer_error_or_secret(self):
        planner = self._planner()
        opportunity = self._opportunity()
        plan = planner.plan(opportunity, self._strategies())

        planner.record_outcome(
            opportunity,
            plan["selected_strategy"],
            outcome="writer_error",
            promotion_performed=False,
        )
        next_plan = planner.plan(opportunity, self._strategies())

        rendered = repr(next_plan)
        self.assertNotIn("raw_error", rendered)
        self.assertNotIn("traceback", rendered.casefold())
        self.assertNotIn("secret", rendered.casefold())
        self.assertEqual(
            set(next_plan["history_summary"]),
            {
                "attempted",
                "rejected_or_failed",
                "promoted",
                "distinct_strategies",
            },
        )


if __name__ == "__main__":
    unittest.main()
