from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.runtime import RuntimeStateStore, run_unified_improvement_cycle


class EngineeringRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        self.generation = 1
        RuntimeStateStore(self.root).save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": None,
            "started_at": "2026-09-21T00:00:00+00:00",
            "heartbeat_at": "2026-09-21T00:00:00+00:00",
            "generation": self.generation,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def selection(self, _root):
        return {
            "selection_id": "ats_fixture",
            "status": "selected",
            "target": {
                "target_kind": "opportunity",
                "opportunity_id": "op_" + "4" * 32,
                "path": "src/app.py",
                "source_sha256": None,
            },
            "alternatives": [],
        }

    def evidence(self, _root, opportunity_id):
        return {
            "evidence_id": "oe_" + "5" * 32,
            "opportunity": {
                "opportunity_id": opportunity_id,
                "fingerprint": "6" * 64,
                "type": "explicit_debt_marker",
                "path": "src/app.py",
                "summary": "fixture engineering opportunity",
            },
            "assessment": {"decision": "research_ready"},
            "success_criteria": {"unit_tests_must_pass": True},
        }

    def research(self, _root, evidence_id):
        return {
            "research_id": "or_" + "7" * 32,
            "opportunity_id": "op_" + "4" * 32,
            "writer_handoff_allowed": True,
            "research_quality": {"decision": "writer_ready"},
            "paid_spending": False,
            "metered_model_requests": 0,
            "api_requests": 0,
        }

    @staticmethod
    def verify(_root, _target, _evidence):
        return {"status": "verified"}

    @staticmethod
    def budget(_root, *, now_epoch=None):
        return {"allowed": True, "reason": "available", "retry_after_seconds": 0}

    def test_engineering_route_is_used_when_explicitly_available(self):
        calls = []

        def engineering_handoff(root, target, evidence, research, **kwargs):
            calls.append(("engineering", kwargs["worker_task_id"]))
            return {
                "kind": "opportunity_writer_handoff",
                "handoff_id": "ohf_" + "8" * 32,
                "runtime_route": "protected_engineering_v1",
                "status": "promoted",
                "outcome": "promotion_committed",
                "promotion_performed": True,
                "main_tree_modified": True,
                "writer_report": {"api_requests": 1},
                "promotion": {"promotion_id": "epr_" + "9" * 32},
            }

        def legacy(*_args, **_kwargs):
            raise AssertionError("legacy handoff must not run for eligible engineering route")

        result = run_unified_improvement_cycle(
            self.root,
            self.generation,
            target_selector=self.selection,
            evidence_builder=self.evidence,
            opportunity_researcher=self.research,
            opportunity_verifier=self.verify,
            opportunity_handoff_runner=legacy,
            engineering_handoff_runner=engineering_handoff,
            metered_budget_checker=self.budget,
            metered_attempt_recorder=lambda *_a, **_k: {"recorded": True},
            code_capability_preflight=lambda _r: {
                "status": "ready",
                "selected_provider_id": "gemini",
            },
        )
        self.assertEqual(result["outcome"], "promotion_committed")
        self.assertTrue(result["promotion_performed"])
        self.assertTrue(result["main_tree_modified"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["promotion_id"].startswith("epr_"))

    def test_stop_from_engineering_handoff_becomes_stop_requested(self):
        def engineering_handoff(root, target, evidence, research, **kwargs):
            return {
                "kind": "opportunity_writer_handoff",
                "handoff_id": "ohf_" + "a" * 32,
                "runtime_route": "protected_engineering_v1",
                "status": "stopped_by_request",
                "outcome": "stop_requested",
                "promotion_performed": False,
                "main_tree_modified": False,
                "writer_report": {"api_requests": 1},
                "promotion": None,
            }

        result = run_unified_improvement_cycle(
            self.root,
            self.generation,
            target_selector=self.selection,
            evidence_builder=self.evidence,
            opportunity_researcher=self.research,
            opportunity_verifier=self.verify,
            opportunity_handoff_runner=lambda *_a, **_k: {},
            engineering_handoff_runner=engineering_handoff,
            metered_budget_checker=self.budget,
            metered_attempt_recorder=lambda *_a, **_k: {"recorded": True},
            code_capability_preflight=lambda _r: {
                "status": "ready",
                "selected_provider_id": "gemini",
            },
        )
        self.assertEqual(result["status"], "stopped_by_request")
        self.assertEqual(result["outcome"], "stop_requested")
        self.assertFalse(result["promotion_performed"])


if __name__ == "__main__":
    unittest.main()
