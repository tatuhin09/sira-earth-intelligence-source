from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sira.models import ProviderError
from sira.runtime import (
    RuntimeStateStore,
    run_autonomous_loop,
    run_one_improvement_cycle,
    run_unified_improvement_cycle,
)

GENERATION = 77


class PersistentRuntimeOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = RuntimeStateStore(self.root)
        self._set_on()

    def tearDown(self):
        self.tmp.cleanup()

    def _set_on(self):
        self.store.save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": os.getpid(),
            "started_at": "2026-09-21T00:00:00+00:00",
            "heartbeat_at": "2026-09-21T00:00:00+00:00",
            "generation": GENERATION,
        })

    @staticmethod
    def _target():
        return {
            "target_kind": "opportunity",
            "priority_class": 2,
            "priority_class_name": "proactive_opportunity",
            "opportunity_id": "op_" + "7" * 32,
            "fingerprint": "8" * 64,
            "type": "complex_function",
            "path": "src/sira/example.py",
            "symbol": "target",
            "priority_score": 430,
            "effective_priority_score": 430,
            "source_sha256": "9" * 64,
            "evidence": {"branch_points": 34, "loc": 170},
            "cooldown": {
                "eligible": True,
                "last_attempt_at": None,
                "remaining_seconds": 0,
            },
            "lineage": {
                "eligible": True,
                "remaining_seconds": 0,
                "priority_penalty": 0,
            },
        }

    def _provider_failure_cycle(self, *, keep_runtime_on):
        target = self._target()

        def selector(_root):
            return {
                "selection_id": "ats_" + "a" * 32,
                "status": "selected",
                "target": target,
            }

        def evidence_builder(_root, _opportunity_id):
            return {
                "evidence_id": "oe_" + "b" * 32,
                "assessment": {"decision": "research_ready"},
            }

        def researcher(_root, _evidence_id):
            return {
                "research_id": "or_" + "c" * 32,
                "writer_handoff_allowed": True,
                "research_quality": {"decision": "writer_ready"},
                "api_requests": 0,
                "metered_model_requests": 0,
            }

        def verifier(_root, _target, _evidence):
            return {
                "status": "verified",
                "checks": {"fixture": True},
                "api_requests": 0,
                "metered_model_requests": 0,
                "main_tree_modified": False,
            }

        def handoff(_root, _research_id):
            raise ProviderError(
                "network_or_timeout",
                True,
                request_count=3,
            )

        def budget(_root, *, now_epoch=None):
            return {
                "allowed": True,
                "reason": "available",
                "retry_after_seconds": 0,
                "attempts_in_window": 0,
                "attempt_limit": 3,
                "window_seconds": 900,
                "minimum_interval_seconds": 120,
            }

        return run_unified_improvement_cycle(
            self.root,
            GENERATION,
            target_selector=selector,
            evidence_builder=evidence_builder,
            opportunity_researcher=researcher,
            opportunity_verifier=verifier,
            opportunity_handoff_runner=handoff,
            metered_budget_checker=budget,
            metered_attempt_recorder=lambda *_args, **_kwargs: {
                "recorded": True
            },
            keep_runtime_on=keep_runtime_on,
        )

    def test_persistent_provider_failure_keeps_cycle_ownership_for_scheduler(self):
        result = self._provider_failure_cycle(keep_runtime_on=True)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["outcome"], "code_worker_failed")
        self.assertEqual(result["error"]["type"], "ProviderError")
        self.assertEqual(result["error"]["code"], "network_or_timeout")

        state, health = self.store._load_raw()
        self.assertEqual(health, "ok")
        self.assertEqual(state["desired_state"], "on")
        self.assertEqual(state["worker_state"], "running")
        self.assertEqual(state["pid"], os.getpid())
        self.assertEqual(state["generation"], GENERATION)

    def test_standalone_provider_failure_still_fails_closed(self):
        result = self._provider_failure_cycle(keep_runtime_on=False)

        self.assertEqual(result["status"], "failed")
        state, health = self.store._load_raw()
        self.assertEqual(health, "ok")
        self.assertEqual(state["desired_state"], "off")
        self.assertEqual(state["worker_state"], "error")
        self.assertIsNone(state["pid"])

    def test_persistent_memory_cycle_failure_does_not_preempt_scheduler_policy(self):
        memory_id = "m_" + "f" * 32

        def planner(_root):
            return {
                "plan_id": "pl_" + "1" * 32,
                "target": {"memory_id": memory_id},
            }

        def researcher(_root, _memory_id):
            raise RuntimeError("fixture memory-cycle failure")

        result = run_one_improvement_cycle(
            self.root,
            GENERATION,
            planner=planner,
            researcher=researcher,
            keep_runtime_on=True,
        )

        self.assertEqual(result["status"], "failed")
        state, health = self.store._load_raw()
        self.assertEqual(health, "ok")
        self.assertEqual(state["desired_state"], "on")
        self.assertEqual(state["worker_state"], "running")
        self.assertEqual(state["pid"], os.getpid())

    def test_outer_scheduler_continues_after_transient_then_honors_stop(self):
        calls = []

        def cycle_runner(_root, generation, *, keep_runtime_on=False):
            calls.append((generation, keep_runtime_on))
            if len(calls) == 1:
                return {
                    "status": "failed",
                    "outcome": "code_worker_failed",
                    "error": {
                        "type": "ProviderError",
                        "code": "network_or_timeout",
                        "request_sent": True,
                        "request_count": 3,
                    },
                    "resource_usage": {
                        "free_public_api_requests": 0,
                        "metered_model_requests": 3,
                    },
                }

            current, health = self.store._load_raw()
            self.assertEqual(health, "ok")
            self.store.save({
                "desired_state": "off",
                "worker_state": "stopping",
                "pid": os.getpid(),
                "started_at": current.get("started_at"),
                "heartbeat_at": current.get("heartbeat_at"),
                "generation": generation,
            })
            return {
                "status": "stopped_by_request",
                "outcome": "stop_requested",
                "resource_usage": {
                    "free_public_api_requests": 0,
                    "metered_model_requests": 0,
                },
            }

        report = run_autonomous_loop(
            self.root,
            GENERATION,
            cycle_interval=0,
            sleep_fn=lambda _seconds: None,
            cycle_runner=cycle_runner,
        )

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(keep for _, keep in calls))
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["cycles_completed"], 2)

        state, health = self.store._load_raw()
        self.assertEqual(health, "ok")
        self.assertEqual(state["desired_state"], "off")
        self.assertEqual(state["worker_state"], "stopped")
        self.assertIsNone(state["pid"])

    def test_outer_scheduler_still_fail_closes_internal_failure(self):
        def cycle_runner(_root, _generation, *, keep_runtime_on=False):
            self.assertTrue(keep_runtime_on)
            return {
                "status": "failed",
                "outcome": "cycle_error",
                "error": {
                    "type": "RuntimeError",
                    "message": "fixture internal failure",
                },
                "resource_usage": {
                    "free_public_api_requests": 0,
                    "metered_model_requests": 0,
                },
            }

        report = run_autonomous_loop(
            self.root,
            GENERATION,
            cycle_interval=0,
            sleep_fn=lambda _seconds: None,
            cycle_runner=cycle_runner,
        )

        self.assertEqual(report["status"], "worker_error")
        self.assertEqual(report["cycles_completed"], 1)
        self.assertEqual(
            report["last_reliability"]["failure_class"],
            "internal",
        )
        self.assertFalse(
            report["last_reliability"]["worker_should_continue"]
        )

        state, health = self.store._load_raw()
        self.assertEqual(health, "ok")
        self.assertEqual(state["desired_state"], "off")
        self.assertEqual(state["worker_state"], "stopped")
        self.assertIsNone(state["pid"])


if __name__ == "__main__":
    unittest.main()
