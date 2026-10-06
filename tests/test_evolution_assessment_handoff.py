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

from sira.opportunity_handoff import run_opportunity_writer_handoff
from sira.storage import write_json


class EvolutionAssessmentHandoffTests(unittest.TestCase):
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
                "summary": "Example complexity.",
            },
            "target": {"path": "src/sira/example.py", "symbol": "example"},
            "success_criteria": {
                "structural_goal": {"metric": "branch_points", "baseline": 1, "target_max": 0},
                "benchmark_suite": "improvement",
            },
        }
        write_json(
            self.root / "improvements/opportunities/evidence" / f"{self.evidence_id}.json",
            evidence,
        )
        strategies = [
            {"strategy": "Extract cohesive phases.", "basis": "research", "citation_ids": ["R1"]},
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
                "structural_goal": {"metric": "branch_points", "baseline": 1, "target_max": 0},
            },
            "citations": [],
            "candidate_strategies": strategies,
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
    def _no_gain():
        return {
            "status": "rejected_structural_goal",
            "outcome": "structural_goal_not_met",
            "promotion_performed": False,
            "main_tree_modified": False,
            "writer_report": {"status": "completed"},
            "structural_check": {
                "metric": "branch_points",
                "baseline": 1,
                "candidate": 1,
                "decision": "fail",
                "decision_code": "structural_goal_not_met",
            },
            "baseline": None,
            "candidate": None,
            "evaluator2": None,
            "evaluator1": None,
            "promotion": None,
        }

    def test_no_gain_assessment_is_persisted_and_consumes_strategy(self):
        with patch(
            "sira.opportunity_handoff.run_autonomous_candidate_promotion",
            return_value=self._no_gain(),
        ):
            first = run_opportunity_writer_handoff(
                self.root, self.research_id, writer=object()
            )
            second = run_opportunity_writer_handoff(
                self.root, self.research_id, writer=object()
            )

        self.assertEqual(first["evolution_assessment"]["classification"], "measured_no_gain")
        self.assertTrue(first["evolution_assessment"]["strategy_consumed"])
        self.assertIsNotNone(first["evolution_outcome"])
        self.assertEqual(second["status"], "strategy_exhausted")
        self.assertIsNotNone(second["capability_gap"])
        self.assertEqual(second["capability_gap"]["gap_kind"], "strategy_exhausted")
        self.assertIsNotNone(second["cooldown"])

    def test_exhaustion_gap_does_not_authorize_any_capability(self):
        with patch(
            "sira.opportunity_handoff.run_autonomous_candidate_promotion",
            return_value=self._no_gain(),
        ):
            run_opportunity_writer_handoff(self.root, self.research_id, writer=object())
            exhausted = run_opportunity_writer_handoff(
                self.root, self.research_id, writer=object()
            )

        gap = exhausted["capability_gap"]
        self.assertFalse(gap["authority_granted"])
        self.assertFalse(gap["external_access_requested"])
        self.assertFalse(gap["promotion_authorized"])
        rendered = repr(exhausted).casefold()
        self.assertNotIn("output_tail", rendered)


if __name__ == "__main__":
    unittest.main()
