from pathlib import Path
import hashlib
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.code_writer import CodeModelBatch
from sira.engineering_runtime import (
    engineering_runtime_eligible,
    run_engineering_runtime_handoff,
)
from sira.engineering_writer import EngineeringResearchBackedWriter


class FixtureModel:
    name = "fixture_engineering_model"
    model_id = "fixture-v1"

    def generate_patch(self, payload, schema):
        return CodeModelBatch(
            {
                "summary": "fixture",
                "edits": [{
                    "path": "src/app.py",
                    "content": "def value():\n    return 2\n",
                    "reason": "fixture",
                }],
                "needs_more_context": False,
                "context_requests": [],
            },
            api_requests=0,
            input_tokens=0,
            output_tokens=0,
        )


def runner(argv, *, cwd, timeout_seconds, max_output_bytes):
    return {
        "status": "passed",
        "returncode": 0,
        "timed_out": False,
        "output_limit_exceeded": False,
        "output_bytes": 0,
        "output_sha256": hashlib.sha256(b"").hexdigest(),
        "duration_ms": 1,
        "diagnostic_text": "",
    }


class EngineeringRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src/app.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        (self.root / "tests/test_app.py").write_text(
            "from src.app import value\n", encoding="utf-8"
        )
        self.target = {
            "target_kind": "opportunity",
            "opportunity_id": "op_" + "1" * 32,
            "path": "src/app.py",
        }
        self.evidence = {
            "opportunity": {
                "opportunity_id": self.target["opportunity_id"],
                "fingerprint": "2" * 64,
                "type": "explicit_debt_marker",
                "path": "src/app.py",
                "symbol": "value",
                "summary": "improve the selected fixture",
            },
            "assessment": {"decision": "research_ready"},
            "success_criteria": {
                "preserve_current_observable_behavior": True,
                "unit_tests_must_pass": True,
            },
        }
        self.research = {
            "research_id": "or_" + "3" * 32,
            "writer_handoff_allowed": True,
            "research_quality": {"decision": "writer_ready"},
            "paid_spending": False,
            "metered_model_requests": 0,
            "candidate_strategies": ["small coherent change"],
            "citations": [],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def writer(self, root):
        return EngineeringResearchBackedWriter(root, FixtureModel())

    def test_clean_engineering_route_promotes_through_protected_chain(self):
        result = run_engineering_runtime_handoff(
            self.root,
            self.target,
            self.evidence,
            self.research,
            worker_task_id="mw_fixture",
            runtime_guard=lambda: True,
            writer_factory=self.writer,
            command_runner=runner,
            state_root=self.base / "external",
        )
        self.assertEqual(result["status"], "promoted")
        self.assertEqual(result["outcome"], "promotion_committed")
        self.assertTrue(result["promotion_performed"])
        self.assertTrue(result["main_tree_modified"])
        self.assertEqual(
            result["promotion"]["decision_code"],
            "engineering_promotion_committed",
        )
        self.assertTrue(Path(result["artifact"]).is_file())
        self.assertIn(
            "return 2",
            (self.root / "src/app.py").read_text(encoding="utf-8"),
        )

    def test_runtime_stop_after_authorization_prevents_promotion(self):
        result = run_engineering_runtime_handoff(
            self.root,
            self.target,
            self.evidence,
            self.research,
            worker_task_id="mw_fixture",
            runtime_guard=lambda: False,
            writer_factory=self.writer,
            command_runner=runner,
            state_root=self.base / "external",
        )
        self.assertEqual(result["status"], "stopped_by_request")
        self.assertEqual(result["outcome"], "stop_requested")
        self.assertFalse(result["promotion_performed"])
        self.assertFalse(result["main_tree_modified"])
        self.assertIn(
            "return 1",
            (self.root / "src/app.py").read_text(encoding="utf-8"),
        )

    def test_protected_target_is_not_eligible(self):
        protected = dict(self.evidence)
        protected["opportunity"] = {
            **self.evidence["opportunity"],
            "path": "src/sira/engineering_promotion.py",
        }
        target = {**self.target, "path": "src/sira/engineering_promotion.py"}
        self.assertFalse(
            engineering_runtime_eligible(
                self.root,
                target,
                protected,
                self.research,
            )
        )

    def test_non_writer_ready_research_is_not_eligible(self):
        research = {
            **self.research,
            "research_quality": {"decision": "insufficient"},
        }
        self.assertFalse(
            engineering_runtime_eligible(
                self.root,
                self.target,
                self.evidence,
                research,
            )
        )


if __name__ == "__main__":
    unittest.main()
