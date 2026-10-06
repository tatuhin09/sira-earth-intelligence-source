from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.access_requests import AccessRequestStore
from sira.runtime import RuntimeStateStore, run_unified_improvement_cycle


class CapabilityBrokerModelRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        RuntimeStateStore(self.root).save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": None,
            "started_at": None,
            "heartbeat_at": None,
            "generation": 1,
        })

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def target():
        return {
            "target_kind": "opportunity",
            "opportunity_id": "op_" + "1" * 32,
            "type": "complex_function",
            "path": "src/sira/example.py",
            "symbol": "example",
            "priority_score": 100,
        }

    def selection(self, _root):
        return {
            "selection_id": "ats_" + "2" * 32,
            "status": "selected",
            "target": self.target(),
            "alternatives": [],
        }

    @staticmethod
    def evidence(_root, opportunity_id):
        return {
            "evidence_id": "oe_" + "3" * 32,
            "opportunity": {"opportunity_id": opportunity_id},
            "assessment": {"decision": "research_ready"},
        }

    @staticmethod
    def research(_root, _evidence_id):
        return {
            "research_id": "or_" + "4" * 32,
            "provider_failures": [],
            "research_quality": {"decision": "writer_ready"},
            "writer_handoff_allowed": True,
            "api_requests": 0,
            "metered_model_requests": 0,
            "paid_spending": False,
        }

    @staticmethod
    def verify(_root, _target, _evidence):
        return {"status": "verified"}

    @staticmethod
    def allowed_budget(*_args, **_kwargs):
        return {"allowed": True, "reason": "available", "retry_after_seconds": 0}

    @staticmethod
    def credential_required(_root):
        return {
            "schema": "sira.capability_broker_decision.v1",
            "decision_id": "cb_" + "5" * 32,
            "capability": "code_generation",
            "status": "credential_required",
            "selected_provider_id": None,
            "fallback_provider_ids": [],
            "access_need": {
                "kind": "credential",
                "resource": "provider:gemini",
                "reason": "Gemini credential is required for code generation.",
                "risk": "low",
                "provider_id": "gemini",
                "credential_name": "GEMINI_API_KEY",
                "owner_action": "Configure GEMINI_API_KEY locally.",
            },
            "owner_action_required": True,
            "allow_metered": True,
            "api_requests": 0,
            "paid_spending": False,
            "provider_execution_performed": False,
            "access_request_created": False,
            "authority_granted": False,
            "promotion_authorized": False,
            "artifact": None,
        }

    @staticmethod
    def ready(_root):
        return {
            "schema": "sira.capability_broker_decision.v1",
            "decision_id": "cb_" + "6" * 32,
            "capability": "code_generation",
            "status": "ready",
            "selected_provider_id": "gemini",
            "fallback_provider_ids": [],
            "access_need": None,
            "owner_action_required": False,
            "allow_metered": True,
            "api_requests": 0,
            "paid_spending": False,
            "provider_execution_performed": False,
            "access_request_created": False,
            "authority_granted": False,
            "promotion_authorized": False,
            "artifact": None,
        }

    @staticmethod
    def budget_blocked(_root):
        return {
            "schema": "sira.capability_broker_decision.v1",
            "decision_id": "cb_" + "7" * 32,
            "capability": "code_generation",
            "status": "metered_budget_blocked",
            "selected_provider_id": None,
            "fallback_provider_ids": [],
            "access_need": None,
            "owner_action_required": False,
            "allow_metered": True,
            "api_requests": 0,
            "paid_spending": False,
            "provider_execution_performed": False,
            "access_request_created": False,
            "authority_granted": False,
            "promotion_authorized": False,
            "artifact": None,
        }

    def test_missing_model_key_defers_to_existing_owner_access_boundary_without_attempt(self):
        attempts, handoffs = [], []
        result = run_unified_improvement_cycle(
            self.root,
            1,
            target_selector=self.selection,
            evidence_builder=self.evidence,
            opportunity_researcher=self.research,
            opportunity_verifier=self.verify,
            opportunity_handoff_runner=lambda *_args: handoffs.append(1) or {},
            metered_budget_checker=self.allowed_budget,
            metered_attempt_recorder=lambda *_args, **_kwargs: attempts.append(1) or {"recorded": True},
            code_capability_preflight=self.credential_required,
            keep_runtime_on=False,
        )
        self.assertEqual(result["status"], "deferred_owner_access")
        self.assertEqual(result["outcome"], "owner_access_required")
        self.assertIsInstance(result["access_request_id"], str)
        self.assertEqual(attempts, [])
        self.assertEqual(handoffs, [])
        row = AccessRequestStore(self.root).load(result["access_request_id"])
        self.assertEqual(row["need"]["provider_id"], "gemini")
        self.assertEqual(row["need"]["credential_name"], "GEMINI_API_KEY")

    def test_ready_model_capability_records_exactly_one_attempt_before_handoff(self):
        events = []

        def record(*_args, **_kwargs):
            events.append("attempt")
            return {"recorded": True}

        def handoff(_root, _research_id):
            events.append("handoff")
            return {
                "status": "completed",
                "outcome": "candidate_rejected",
                "promotion_performed": False,
                "main_tree_modified": False,
                "writer_report": {"api_requests": 1},
            }

        result = run_unified_improvement_cycle(
            self.root,
            1,
            target_selector=self.selection,
            evidence_builder=self.evidence,
            opportunity_researcher=self.research,
            opportunity_verifier=self.verify,
            opportunity_handoff_runner=handoff,
            metered_budget_checker=self.allowed_budget,
            metered_attempt_recorder=record,
            code_capability_preflight=self.ready,
            keep_runtime_on=False,
        )
        self.assertEqual(events, ["attempt", "handoff"])
        self.assertIsNone(result.get("access_request_id"))
        self.assertEqual(result["resource_usage"]["metered_model_requests"], 1)

    def test_model_budget_block_is_non_access_deferral_and_records_no_attempt(self):
        attempts = []
        result = run_unified_improvement_cycle(
            self.root,
            1,
            target_selector=self.selection,
            evidence_builder=self.evidence,
            opportunity_researcher=self.research,
            opportunity_verifier=self.verify,
            opportunity_handoff_runner=lambda *_args: {"status": "should_not_run"},
            metered_budget_checker=self.allowed_budget,
            metered_attempt_recorder=lambda *_args, **_kwargs: attempts.append(1) or {"recorded": True},
            code_capability_preflight=self.budget_blocked,
            keep_runtime_on=False,
        )
        self.assertEqual(result["status"], "deferred_resource_budget")
        self.assertEqual(result["outcome"], "model_capability_metered_budget_blocked")
        self.assertIsNone(result.get("access_request_id"))
        self.assertEqual(attempts, [])


if __name__ == "__main__":
    unittest.main()
