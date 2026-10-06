from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SRC_TEXT = str(SRC)
sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]

from sira.models import ProviderError
from sira.opportunity_handoff import run_opportunity_writer_handoff
from sira.storage import write_json


class EvolutionHandoffIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        source = self.root / "src/sira/example.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "def example(value):\n"
            "    if value:\n"
            "        return value\n"
            "    return None\n",
            encoding="utf-8",
        )
        sha = hashlib.sha256(source.read_bytes()).hexdigest()

        self.evidence_id = "oe_" + "1" * 32
        self.research_id = "or_" + "2" * 32
        self.opportunity_id = "op_" + "3" * 32

        evidence = {
            "schema_version": 1,
            "kind": "opportunity_evidence_brief",
            "evidence_id": self.evidence_id,
            "assessment": {"decision": "research_ready"},
            "opportunity": {
                "opportunity_id": self.opportunity_id,
                "fingerprint": "4" * 64,
                "type": "complex_function",
                "path": "src/sira/example.py",
                "symbol": "example",
                "source_sha256": sha,
                "summary": "Example function has branch-heavy responsibilities.",
            },
            "target": {
                "path": "src/sira/example.py",
                "symbol": "example",
            },
            "success_criteria": {
                "structural_goal": {
                    "metric": "branch_points",
                    "baseline": 1,
                    "target_max": 0,
                },
                "benchmark_suite": "improvement",
            },
        }
        write_json(
            self.root / "improvements/opportunities/evidence" / f"{self.evidence_id}.json",
            evidence,
        )

        self.strategies = [
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
        ]
        research = {
            "schema_version": 1,
            "kind": "opportunity_free_research_brief",
            "research_id": self.research_id,
            "evidence_id": self.evidence_id,
            "opportunity_id": self.opportunity_id,
            "target": {
                "path": "src/sira/example.py",
                "symbol": "example",
                "benchmark_suite": "improvement",
                "structural_goal": {
                    "metric": "branch_points",
                    "baseline": 1,
                    "target_max": 0,
                },
            },
            "citations": [],
            "candidate_strategies": self.strategies,
            "research_quality": {"decision": "writer_ready"},
            "writer_handoff_allowed": True,
            "paid_spending": False,
            "metered_model_requests": 0,
        }
        write_json(
            self.root / "improvements/opportunities/research" / f"{self.research_id}.json",
            research,
        )

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _rejected_attempt():
        return {
            "status": "rejected_structural_goal",
            "outcome": "structural_goal_not_met",
            "promotion_performed": False,
            "main_tree_modified": False,
            "writer_report": {"status": "completed"},
            "structural_check": {"decision": "fail"},
            "evaluator2": None,
            "evaluator1": None,
            "promotion": None,
        }

    @staticmethod
    def _promoted_attempt():
        return {
            "status": "promoted",
            "outcome": "promotion_committed",
            "promotion_performed": True,
            "main_tree_modified": True,
            "writer_report": {"status": "completed"},
            "structural_check": {"decision": "pass"},
            "evaluator2": {"decision": "accept"},
            "evaluator1": {"decision": "allow"},
            "promotion": {"status": "promoted", "promotion_id": "pr_" + "5" * 32},
        }

    def test_rejected_strategy_is_consumed_and_next_handoff_uses_distinct_strategy(self):
        seen = []

        def fake_promotion(_root, hypothesis, **_kwargs):
            seen.append(hypothesis)
            return self._rejected_attempt()

        with patch(
            "sira.opportunity_handoff.run_autonomous_candidate_promotion",
            side_effect=fake_promotion,
        ):
            first = run_opportunity_writer_handoff(self.root, self.research_id, writer=object())
            second = run_opportunity_writer_handoff(self.root, self.research_id, writer=object())

        self.assertEqual(first["evolution_plan"]["selected_index"], 0)
        self.assertEqual(second["evolution_plan"]["selected_index"], 1)
        self.assertEqual(len(seen), 2)
        self.assertEqual(
            seen[0]["opportunity_context"]["selected_strategy"]["strategy"],
            self.strategies[0]["strategy"],
        )
        self.assertEqual(
            seen[1]["opportunity_context"]["selected_strategy"]["strategy"],
            self.strategies[1]["strategy"],
        )
        self.assertIsNotNone(first["evolution_outcome"])
        self.assertIsNotNone(second["evolution_outcome"])

    def test_strategy_exhaustion_stops_before_writer_or_promotion(self):
        with patch(
            "sira.opportunity_handoff.run_autonomous_candidate_promotion",
            return_value=self._rejected_attempt(),
        ) as promote:
            run_opportunity_writer_handoff(self.root, self.research_id, writer=object())
            run_opportunity_writer_handoff(self.root, self.research_id, writer=object())
            exhausted = run_opportunity_writer_handoff(self.root, self.research_id, writer=object())

        self.assertEqual(promote.call_count, 2)
        self.assertEqual(exhausted["status"], "strategy_exhausted")
        self.assertEqual(exhausted["outcome"], "strategy_exhausted")
        self.assertFalse(exhausted["promotion_performed"])
        self.assertIsNone(exhausted["evolution_outcome"])

    def test_successful_promotion_suppresses_same_lineage_before_next_writer(self):
        with patch(
            "sira.opportunity_handoff.run_autonomous_candidate_promotion",
            return_value=self._promoted_attempt(),
        ) as promote:
            first = run_opportunity_writer_handoff(self.root, self.research_id, writer=object())
            suppressed = run_opportunity_writer_handoff(self.root, self.research_id, writer=object())

        self.assertEqual(first["outcome"], "promotion_committed")
        self.assertEqual(promote.call_count, 1)
        self.assertEqual(suppressed["status"], "recent_success_suppressed")
        self.assertFalse(suppressed["promotion_performed"])
        self.assertIsNone(suppressed["evolution_outcome"])

    def test_provider_or_writer_error_does_not_consume_strategy(self):
        with patch(
            "sira.opportunity_handoff.run_autonomous_candidate_promotion",
            side_effect=ProviderError("http_503", True, request_count=1),
        ):
            failed = run_opportunity_writer_handoff(self.root, self.research_id, writer=object())

        self.assertEqual(failed["status"], "writer_error")
        self.assertIsNone(failed["evolution_outcome"])

        with patch(
            "sira.opportunity_handoff.run_autonomous_candidate_promotion",
            return_value=self._rejected_attempt(),
        ):
            retried = run_opportunity_writer_handoff(self.root, self.research_id, writer=object())

        self.assertEqual(retried["evolution_plan"]["selected_index"], 0)

    def test_evolution_report_is_aggregate_and_selected_strategy_only(self):
        with patch(
            "sira.opportunity_handoff.run_autonomous_candidate_promotion",
            return_value=self._rejected_attempt(),
        ):
            report = run_opportunity_writer_handoff(self.root, self.research_id, writer=object())

        context = report["attempt"]
        rendered = repr(report).casefold()
        self.assertNotIn("raw_error", rendered)
        self.assertNotIn("traceback", rendered)
        self.assertIn("evolution_plan", report)
        self.assertIn("evolution_outcome", report)
        self.assertEqual(report["evolution_plan"]["selected_index"], 0)


if __name__ == "__main__":
    unittest.main()
