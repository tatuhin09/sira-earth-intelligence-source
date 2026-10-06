from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

# SIRA_TEST_SRC_PATH_FIX_V1
import sys
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SRC_TEXT = str(SRC)
sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]

from sira.memory import MemoryStore, Observation


class StrategyLearnerContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _real_memory(
        self,
        *,
        signature: str,
        summary: str,
        capability: str = "scholarly_research",
        provider: str = "semantic_scholar",
    ) -> tuple[MemoryStore, str]:
        store = MemoryStore(self.root)
        memory_id, _ = store.upsert(
            Observation(
                "success",
                "success",
                capability,
                summary,
                "validated",
                provider=provider,
                signature=signature,
            ),
            run_id=(signature[-2:] or "aa") * 16,
            artifact_name="result.json",
            artifact_sha256=(signature[0] if signature else "a") * 64,
            outcome_status="completed",
        )
        return store, memory_id

    def _candidates(self):
        return [
            {
                "strategy_id": "semantic_scholar",
                "base_score": 0.70,
                "available": True,
                "policy_allowed": True,
                "metered": False,
            },
            {
                "strategy_id": "crossref",
                "base_score": 0.70,
                "available": True,
                "policy_allowed": True,
                "metered": False,
            },
            {
                "strategy_id": "tavily",
                "base_score": 0.72,
                "available": True,
                "policy_allowed": False,
                "metered": True,
            },
        ]

    def _learner(self):
        from sira.strategy_learner import StrategyLearner
        return StrategyLearner(self.root)

    def test_no_history_uses_deterministic_policy_safe_fallback(self):
        result = self._learner().rank(
            "scholarly_research",
            self._candidates(),
        )
        self.assertEqual(result["schema"], "sira.strategy_decision.v1")
        self.assertEqual(result["recommended_strategy_id"], "crossref")
        self.assertFalse(result["used_learning"])
        self.assertEqual(result["learning_event_count"], 0)
        tavily = next(
            row for row in result["ranking"]
            if row["strategy_id"] == "tavily"
        )
        self.assertFalse(tavily["eligible"])
        self.assertIn("policy_blocked", tavily["reasons"])

    def test_two_successful_real_outcomes_create_bounded_positive_adjustment(self):
        store, memory_id = self._real_memory(
            signature="sa-success",
            summary="Semantic Scholar strategy succeeded",
        )
        for _ in range(2):
            store.record_outcome(
                memory_id,
                "application_succeeded",
                context={
                    "strategy_id": "semantic_scholar",
                    "capability": "scholarly_research",
                },
                weight=1.0,
            )

        result = self._learner().rank(
            "scholarly_research",
            self._candidates(),
        )
        semantic = next(
            row for row in result["ranking"]
            if row["strategy_id"] == "semantic_scholar"
        )
        self.assertTrue(result["used_learning"])
        self.assertEqual(result["recommended_strategy_id"], "semantic_scholar")
        self.assertGreater(semantic["learned_adjustment"], 0.0)
        self.assertLessEqual(semantic["learned_adjustment"], 0.15)
        self.assertEqual(semantic["evidence"]["positive"], 2)
        self.assertEqual(semantic["evidence"]["negative"], 0)

    def test_single_outcome_is_below_learning_threshold(self):
        store, memory_id = self._real_memory(
            signature="one-event",
            summary="One isolated provider success",
        )
        store.record_outcome(
            memory_id,
            "application_succeeded",
            context={
                "strategy_id": "semantic_scholar",
                "capability": "scholarly_research",
            },
            weight=1.0,
        )
        result = self._learner().rank(
            "scholarly_research",
            self._candidates(),
        )
        semantic = next(
            row for row in result["ranking"]
            if row["strategy_id"] == "semantic_scholar"
        )
        self.assertEqual(semantic["learned_adjustment"], 0.0)
        self.assertFalse(result["used_learning"])
        self.assertIn("insufficient_evidence", semantic["reasons"])

    def test_repeated_negative_outcomes_penalize_strategy_without_disabling_it(self):
        store, memory_id = self._real_memory(
            signature="negative-events",
            summary="Semantic Scholar repeated failure",
        )
        for _ in range(3):
            store.record_outcome(
                memory_id,
                "application_failed",
                context={
                    "strategy_id": "semantic_scholar",
                    "capability": "scholarly_research",
                },
                weight=1.0,
            )

        result = self._learner().rank(
            "scholarly_research",
            self._candidates(),
        )
        semantic = next(
            row for row in result["ranking"]
            if row["strategy_id"] == "semantic_scholar"
        )
        self.assertTrue(semantic["eligible"])
        self.assertLess(semantic["learned_adjustment"], 0.0)
        self.assertGreaterEqual(semantic["learned_adjustment"], -0.15)
        self.assertEqual(semantic["evidence"]["negative"], 3)

    def test_synthetic_only_memory_cannot_influence_strategy_ranking(self):
        store = MemoryStore(self.root)
        memory_id, _ = store.upsert(
            Observation(
                "success",
                "success",
                "scholarly_research",
                "fixture strategy success",
                "validated",
                provider="fixture",
                signature="synthetic-strategy",
                origin="synthetic_fixture",
                synthetic=True,
            ),
            run_id="ab" * 16,
            artifact_name="result.json",
            artifact_sha256="a" * 64,
            outcome_status="completed",
        )
        for _ in range(4):
            store.record_outcome(
                memory_id,
                "application_succeeded",
                context={
                    "strategy_id": "semantic_scholar",
                    "capability": "scholarly_research",
                },
                weight=1.0,
            )

        result = self._learner().rank(
            "scholarly_research",
            self._candidates(),
        )
        semantic = next(
            row for row in result["ranking"]
            if row["strategy_id"] == "semantic_scholar"
        )
        self.assertEqual(semantic["learned_adjustment"], 0.0)
        self.assertEqual(semantic["evidence"]["eligible_events"], 0)
        self.assertFalse(result["used_learning"])

    def test_contradicted_memory_contributes_less_than_clean_memory(self):
        store, clean_id = self._real_memory(
            signature="clean-positive",
            summary="clean successful strategy",
        )
        _, contradicted_id = self._real_memory(
            signature="contradicted-positive",
            summary="contradicted successful strategy",
            provider="crossref",
        )

        opposing_id, _ = store.upsert(
            Observation(
                "failure",
                "provider",
                "scholarly_research",
                "Crossref same context failed",
                "observed",
                provider="crossref",
                error_code="provider_error",
                signature="crossref-opposing",
            ),
            run_id="cd" * 16,
            artifact_name="result.json",
            artifact_sha256="c" * 64,
            outcome_status="failed",
        )
        store.relate(
            contradicted_id,
            opposing_id,
            "contradicts",
            0.9,
            "same provider context opposite result",
        )

        for memory_id, strategy_id in (
            (clean_id, "semantic_scholar"),
            (contradicted_id, "crossref"),
        ):
            for _ in range(2):
                store.record_outcome(
                    memory_id,
                    "application_succeeded",
                    context={
                        "strategy_id": strategy_id,
                        "capability": "scholarly_research",
                    },
                    weight=1.0,
                )

        result = self._learner().rank(
            "scholarly_research",
            self._candidates(),
        )
        rows = {row["strategy_id"]: row for row in result["ranking"]}
        self.assertGreater(
            rows["semantic_scholar"]["learned_adjustment"],
            rows["crossref"]["learned_adjustment"],
        )
        self.assertGreater(
            rows["crossref"]["evidence"]["contradicted_event_weight"],
            0.0,
        )

    def test_policy_block_cannot_be_overridden_by_learning(self):
        store, memory_id = self._real_memory(
            signature="blocked-learning",
            summary="Tavily succeeded many times",
            provider="tavily",
        )
        for _ in range(4):
            store.record_outcome(
                memory_id,
                "application_succeeded",
                context={
                    "strategy_id": "tavily",
                    "capability": "scholarly_research",
                },
                weight=1.0,
            )

        result = self._learner().rank(
            "scholarly_research",
            self._candidates(),
        )
        tavily = next(
            row for row in result["ranking"]
            if row["strategy_id"] == "tavily"
        )
        self.assertFalse(tavily["eligible"])
        self.assertNotEqual(result["recommended_strategy_id"], "tavily")
        self.assertIn("policy_blocked", tavily["reasons"])

    def test_strategy_decision_contains_only_aggregate_learning_evidence(self):
        store, memory_id = self._real_memory(
            signature="redaction-check",
            summary="strategy success with sensitive-looking context",
        )
        for _ in range(2):
            store.record_outcome(
                memory_id,
                "retrieval_helped",
                context={
                    "strategy_id": "semantic_scholar",
                    "capability": "scholarly_research",
                    "token": "must-not-be-returned",
                    "raw_error": "must-not-be-returned-either",
                },
                weight=1.0,
            )

        result = self._learner().rank(
            "scholarly_research",
            self._candidates(),
        )
        rendered = repr(result)
        self.assertNotIn("must-not-be-returned", rendered)
        semantic = next(
            row for row in result["ranking"]
            if row["strategy_id"] == "semantic_scholar"
        )
        self.assertEqual(
            set(semantic["evidence"]),
            {
                "eligible_events",
                "positive",
                "negative",
                "neutral",
                "weighted_signal",
                "contradicted_event_weight",
            },
        )


if __name__ == "__main__":
    unittest.main()
