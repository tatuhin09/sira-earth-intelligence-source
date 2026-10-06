from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SRC_TEXT = str(SRC)
sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]

from sira.access_requests import AccessNeed, AccessRequestStore, KIND_CREDENTIAL
from sira.access_runtime import (
    access_need_from_research,
    blocked_target_ids,
    consume_resume_for_target,
    filter_selection_for_access,
    park_target_for_access,
    target_identity,
)
from sira.autonomous_targeting import select_autonomous_target
from sira.runtime import RuntimeStateStore, run_unified_improvement_cycle


class AccessRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _opportunity(suffix: str) -> dict:
        return {
            "target_kind": "opportunity",
            "opportunity_id": "op_" + suffix * 32,
            "type": "complexity",
            "path": "src/sira/example.py",
            "symbol": "target",
            "priority_score": 50,
            "fingerprint": suffix * 64,
        }

    @staticmethod
    def _need() -> AccessNeed:
        return AccessNeed(
            kind=KIND_CREDENTIAL,
            resource="tavily",
            reason="Tavily access is required for this task.",
            provider_id="tavily",
            credential_name="TAVILY_API_KEY",
        )

    def test_pending_and_approved_requests_block_until_satisfied(self):
        target = self._opportunity("a")
        row = park_target_for_access(self.root, target, self._need())

        self.assertIn(target_identity(target), blocked_target_ids(self.root))
        store = AccessRequestStore(self.root)
        store.approve(row["request_id"])
        self.assertIn(target_identity(target), blocked_target_ids(self.root))

        store.mark_satisfied(row["request_id"])
        self.assertNotIn(target_identity(target), blocked_target_ids(self.root))

    def test_satisfied_request_is_consumed_when_target_resumes(self):
        target = self._opportunity("b")
        store = AccessRequestStore(self.root)
        row = park_target_for_access(self.root, target, self._need())
        store.approve(row["request_id"])
        store.mark_satisfied(row["request_id"])

        consumed = consume_resume_for_target(self.root, target)
        self.assertEqual(consumed, [row["request_id"]])
        self.assertEqual(store.resume_ready(), ())

    def test_selection_filter_reselects_alternative(self):
        blocked = self._opportunity("c")
        alternate = self._opportunity("d")
        selection = {
            "selection_id": "ats_" + "1" * 32,
            "status": "selected",
            "target": blocked,
            "alternatives": [alternate],
        }

        result = filter_selection_for_access(
            selection,
            {target_identity(blocked)},
        )
        self.assertEqual(
            result["target"]["opportunity_id"],
            alternate["opportunity_id"],
        )
        self.assertTrue(result["access_reselected"])

    def test_missing_tavily_key_failure_becomes_access_need(self):
        need = access_need_from_research({
            "provider_failures": [
                {"provider": "Tavily", "code": "missing_key"},
            ],
        })
        self.assertIsNotNone(need)
        self.assertEqual(need.kind, KIND_CREDENTIAL)
        self.assertEqual(need.provider_id, "tavily")
        self.assertEqual(need.credential_name, "TAVILY_API_KEY")

    def test_transient_provider_failure_does_not_request_owner_access(self):
        need = access_need_from_research({
            "provider_failures": [
                {"provider": "Tavily", "code": "http_503"},
            ],
        })
        self.assertIsNone(need)

    def test_default_selector_excludes_higher_priority_memory_target(self):
        first = {
            "memory_id": "m_" + "1" * 32,
            "category": "benchmark_regression",
            "priority_score": 100,
            "last_seen_at": "2026-09-20T00:00:00Z",
        }
        second = {
            "memory_id": "m_" + "2" * 32,
            "category": "verification",
            "priority_score": 50,
            "last_seen_at": "2026-09-20T00:00:01Z",
        }

        report = select_autonomous_target(
            self.root,
            memory_candidates_fn=lambda _root, _limit: [first, second],
            opportunity_discovery_fn=lambda _root, _limit, _now: {
                "opportunities": [],
            },
            excluded_target_ids={"memory:" + first["memory_id"]},
        )
        self.assertEqual(report["target"]["memory_id"], second["memory_id"])
        self.assertEqual(report["access_excluded_count"], 1)

    def test_runtime_defers_owner_access_without_cycle_failure(self):
        RuntimeStateStore(self.root).save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": None,
            "started_at": None,
            "heartbeat_at": None,
            "generation": 1,
        })
        target = self._opportunity("e")

        def selector(_root):
            return {
                "selection_id": "ats_" + "a" * 32,
                "status": "selected",
                "target": target,
                "alternatives": [],
            }

        def evidence(_root, opportunity_id):
            return {
                "evidence_id": "oe_" + "a" * 32,
                "opportunity": {"opportunity_id": opportunity_id},
                "assessment": {"decision": "research_ready"},
            }

        def research(_root, _evidence_id):
            return {
                "research_id": "or_" + "b" * 32,
                "provider_failures": [
                    {"provider": "Tavily", "code": "missing_key"},
                ],
                "research_quality": {"decision": "insufficient"},
                "writer_handoff_allowed": False,
                "api_requests": 0,
            }

        def verify(_root, _target, _evidence):
            return {"status": "verified"}

        result = run_unified_improvement_cycle(
            self.root,
            1,
            target_selector=selector,
            evidence_builder=evidence,
            opportunity_researcher=research,
            opportunity_verifier=verify,
            metered_budget_checker=lambda *_args, **_kwargs: {
                "allowed": True,
                "reason": "ready",
                "retry_after_seconds": 0,
            },
            keep_runtime_on=True,
        )

        self.assertEqual(result["status"], "deferred_owner_access")
        self.assertEqual(result["outcome"], "owner_access_required")
        self.assertIsInstance(result["access_request_id"], str)
        self.assertIn(target_identity(target), blocked_target_ids(self.root))
        self.assertEqual(
            result["worker_coordination"]["status"],
            "deferred_owner_access",
        )


if __name__ == "__main__":
    unittest.main()
