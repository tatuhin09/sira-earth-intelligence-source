from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.skill_runtime import attach_skill_context, record_verified_handoff_skill


def success(handoff_id):
    return {
        "handoff_id": handoff_id,
        "status": "promoted",
        "outcome": "promotion_committed",
        "promotion_performed": True,
        "main_tree_modified": True,
        "structural_check": {"decision": "pass"},
    }


class OpportunitySkillReuseTests(unittest.TestCase):
    def test_consolidated_skill_is_advisory_and_preserves_existing_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            opportunity = {"type": "complex_function", "summary": "complex function"}
            for char in ("1", "2", "3"):
                record_verified_handoff_skill(
                    root, opportunity, success("ohf_" + char * 32)
                )
            base = {
                "rationale": "Existing evolution-selected strategy.",
                "opportunity_context": {
                    "selected_strategy_id": "strategy-existing",
                    "candidate_strategies": [{"strategy": "Extract helpers."}],
                },
            }
            enriched = attach_skill_context(root, opportunity, base)

        learned = enriched["opportunity_context"]["consolidated_skill"]
        self.assertEqual(learned["skill_key"], "code.refactor.complex_function")
        self.assertTrue(learned["advisory_only"])
        self.assertEqual(
            enriched["opportunity_context"]["selected_strategy_id"],
            "strategy-existing",
        )
        self.assertIn("Verified reusable skill", enriched["rationale"])

    def test_no_skill_preserves_existing_hypothesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            enriched = attach_skill_context(
                Path(tmp),
                {"type": "complex_function"},
                {
                    "rationale": "Keep current strategy.",
                    "opportunity_context": {"selected_strategy_id": "s1"},
                },
            )
        self.assertIsNone(enriched["opportunity_context"]["consolidated_skill"])
        self.assertEqual(enriched["opportunity_context"]["selected_strategy_id"], "s1")


if __name__ == "__main__":
    unittest.main()
