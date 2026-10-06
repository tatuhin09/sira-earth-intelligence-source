"""The runtime engineering route enforces the opportunity's measurable goal."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_runtime import run_engineering_runtime_handoff


class EngineeringRuntimeStructuralGoalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        source = self.root / "src/sira/feature.py"
        source.parent.mkdir(parents=True)
        source.write_text("def target():\n    if True:\n        return 1\n    return 0\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_feature.py").write_text("# fixture\n", encoding="utf-8")
        self.target = {
            "target_kind": "opportunity", "opportunity_id": "op_" + "1" * 32,
            "path": "src/sira/feature.py",
        }
        self.evidence = {
            "opportunity": {
                "opportunity_id": self.target["opportunity_id"],
                "type": "complex_function", "path": "src/sira/feature.py",
                "symbol": "target", "summary": "Simplify a branch.",
            },
            "assessment": {"decision": "research_ready"},
            "success_criteria": {
                "structural_goal": {"metric": "branch_points", "baseline": 1, "target_max": 0},
            },
        }
        self.research = {
            "research_id": "or_" + "2" * 32,
            "writer_handoff_allowed": True,
            "research_quality": {"decision": "writer_ready"},
            "paid_spending": False, "metered_model_requests": 0,
        }

    def run_candidate(self, content):
        evaluator_calls = []

        def prepare(root, task, writer_workspace, writer, **kwargs):
            candidate = writer_workspace / "edit/candidate"
            target = candidate / "src/sira/feature.py"
            target.parent.mkdir(parents=True)
            target.write_text(content, encoding="utf-8")
            return {"status": "verified_candidate_ready", "candidate_root": str(candidate)}

        def evaluate(root, attempt):
            evaluator_calls.append(True)
            return {"decision": "reject", "decision_code": "fixture_evaluator_stop"}

        result = run_engineering_runtime_handoff(
            self.root, self.target, self.evidence, self.research,
            worker_task_id="mw_fixture", writer_factory=lambda root: object(),
            candidate_preparer=prepare, evaluator=evaluate,
            state_root=Path(self.temp.name) / "external",
        )
        return result, evaluator_calls

    def test_unmet_branch_goal_rejects_before_evaluator_and_promotion(self):
        result, evaluator_calls = self.run_candidate(
            "def target():\n    if True:\n        return 2\n    return 0\n"
        )
        self.assertEqual(result["status"], "rejected_structural_goal")
        self.assertEqual(result["outcome"], "structural_goal_not_met")
        self.assertEqual(result["structural_check"]["candidate"], 1)
        self.assertFalse(evaluator_calls)
        self.assertFalse(result["promotion_performed"])
        self.assertIn("return 1", (self.root / "src/sira/feature.py").read_text())

    def test_met_branch_goal_reaches_evaluator_without_promotion(self):
        result, evaluator_calls = self.run_candidate("def target():\n    return 1\n")
        self.assertEqual(result["structural_check"]["decision"], "pass")
        self.assertEqual(result["structural_check"]["candidate"], 0)
        self.assertEqual(result["outcome"], "fixture_evaluator_stop")
        self.assertEqual(len(evaluator_calls), 1)
        self.assertFalse(result["promotion_performed"])

    def test_invalid_goal_fails_closed_before_evaluator(self):
        self.evidence["success_criteria"]["structural_goal"] = {
            "metric": "unsupported_metric", "baseline": 1, "target_max": 0,
        }
        result, evaluator_calls = self.run_candidate("def target():\n    return 1\n")
        self.assertEqual(result["outcome"], "structural_goal_invalid")
        self.assertFalse(evaluator_calls)
        self.assertFalse(result["promotion_performed"])

    def test_stale_goal_rejects_before_spending_model_request(self):
        self.evidence["success_criteria"]["structural_goal"]["baseline"] = 2

        def unexpected_writer(root):
            self.fail("stale baseline must stop before model construction")

        result = run_engineering_runtime_handoff(
            self.root, self.target, self.evidence, self.research,
            worker_task_id="mw_fixture", writer_factory=unexpected_writer,
            state_root=Path(self.temp.name) / "external",
        )
        self.assertEqual(result["outcome"], "stale_structural_baseline")
        self.assertFalse(result["promotion_performed"])


if __name__ == "__main__":
    unittest.main()
