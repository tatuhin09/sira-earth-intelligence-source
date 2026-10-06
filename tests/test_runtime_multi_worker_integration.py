import os
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.runtime import RuntimeStateStore
from sira.worker_coordination import MultiWorkerCoordinator, PromotionLease, WorkerTaskStore


class RuntimeMultiWorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = RuntimeStateStore(self.root)
        self.store.save({
            "desired_state": "on",
            "worker_state": "starting",
            "pid": os.getpid(),
            "started_at": "2026-09-18T00:00:00+00:00",
            "heartbeat_at": "2026-09-18T00:00:00+00:00",
            "generation": 41,
        })

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _target():
        return {
            "target_kind": "opportunity",
            "priority_class": 2,
            "priority_class_name": "proactive_opportunity",
            "opportunity_id": "op_" + "1" * 32,
            "fingerprint": "2" * 64,
            "type": "complex_function",
            "path": "src/sira/retrieval.py",
            "symbol": "load_evidence_run",
            "priority_score": 401,
            "effective_priority_score": 401,
            "source_sha256": "3" * 64,
            "evidence": {"branch_points": 48, "loc": 71},
            "cooldown": {"eligible": True, "last_attempt_at": None, "remaining_seconds": 0},
            "lineage": {"eligible": True, "remaining_seconds": 0, "priority_penalty": 0},
        }

    def _selector(self, _root):
        return {
            "selection_id": "ats_" + "a" * 32,
            "status": "selected",
            "target": self._target(),
        }

    @staticmethod
    def _evidence(_root, _opportunity_id):
        return {
            "evidence_id": "oe_" + "b" * 32,
            "assessment": {"decision": "research_ready"},
        }

    @staticmethod
    def _research(_root, _evidence_id):
        return {
            "research_id": "or_" + "c" * 32,
            "writer_handoff_allowed": True,
            "research_quality": {"decision": "writer_ready"},
            "api_requests": 2,
            "metered_model_requests": 0,
        }

    @staticmethod
    def _verify(_root, _target, _evidence):
        return {"status": "verified", "checks": {"evidence_ready": True, "target_consistent": True}}

    @staticmethod
    def _budget(_root, *, now_epoch=None):
        return {"allowed": True, "reason": "available", "retry_after_seconds": 0}

    def test_coordinator_dispatch_guard_blocks_code_worker_and_persists_ownership(self):
        coordinator = MultiWorkerCoordinator(self.root)
        code_calls = []

        def readonly(ctx, _target):
            return {"status": "ok", "role": ctx.role}

        report = coordinator.run_task(
            self._target(),
            research_worker=readonly,
            verification_worker=readonly,
            code_worker=lambda *_args: code_calls.append(True) or {"status": "unexpected"},
            budget_checker=lambda _root: {"allowed": True, "reason": "available", "retry_after_seconds": 0},
            dispatch_guard=lambda: False,
        )

        self.assertEqual(report["status"], "abandoned_reselect")
        self.assertEqual(report["outcome"], "dispatch_stopped")
        self.assertEqual(code_calls, [])
        task = WorkerTaskStore(self.root).load(report["task_id"])
        self.assertEqual(task["owner_pid"], os.getpid())
        self.assertIsInstance(task["heartbeat_at"], str)
        self.assertIsInstance(task["workers"]["research"]["heartbeat_at"], str)
        self.assertIsInstance(task["workers"]["verification"]["heartbeat_at"], str)

    def test_opportunity_cycle_routes_readonly_and_code_phases_through_coordinator(self):
        from sira.runtime import run_unified_improvement_cycle

        handoff_calls = []
        attempt_records = []

        def handoff(_root, research_id):
            handoff_calls.append(research_id)
            return {
                "handoff_id": "ohf_" + "d" * 32,
                "status": "promoted",
                "outcome": "promotion_committed",
                "promotion_performed": True,
                "main_tree_modified": True,
                "writer_report": {"api_requests": 1, "attempt_count": 1},
                "promotion": {"promotion_id": "pr_" + "e" * 32},
            }

        result = run_unified_improvement_cycle(
            self.root,
            41,
            target_selector=self._selector,
            evidence_builder=self._evidence,
            opportunity_researcher=self._research,
            opportunity_verifier=self._verify,
            opportunity_handoff_runner=handoff,
            metered_budget_checker=self._budget,
            metered_attempt_recorder=lambda _root, *, now_epoch=None: attempt_records.append(True) or {"recorded": True},
        )

        self.assertEqual(result["sira_runtime_stage"], "1.2")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "promotion_committed")
        self.assertEqual(handoff_calls, ["or_" + "c" * 32])
        self.assertEqual(attempt_records, [True])
        self.assertEqual(result["resource_usage"]["free_public_api_requests"], 2)
        self.assertEqual(result["resource_usage"]["metered_model_requests"], 1)
        coordination = result["worker_coordination"]
        self.assertTrue(coordination["task_id"].startswith("mw_"))
        self.assertEqual(coordination["worker_states"]["research"], "completed")
        self.assertEqual(coordination["worker_states"]["verification"], "completed")
        self.assertEqual(coordination["worker_states"]["code"], "completed")
        self.assertTrue(coordination["promotion_performed"])
        self.assertTrue(coordination["protected_promotion_gate"])

    def test_self_off_during_readonly_phase_prevents_code_dispatch(self):
        from sira.runtime import run_unified_improvement_cycle

        handoff_calls = []

        def research(root, evidence_id):
            current, _ = self.store._load_raw()
            self.store.save({
                "desired_state": "off",
                "worker_state": "stopping",
                "pid": os.getpid(),
                "started_at": current.get("started_at"),
                "heartbeat_at": current.get("heartbeat_at"),
                "generation": 41,
            })
            return self._research(root, evidence_id)

        result = run_unified_improvement_cycle(
            self.root,
            41,
            target_selector=self._selector,
            evidence_builder=self._evidence,
            opportunity_researcher=research,
            opportunity_verifier=self._verify,
            opportunity_handoff_runner=lambda *_args: handoff_calls.append(True) or {},
            metered_budget_checker=self._budget,
        )

        self.assertEqual(result["status"], "stopped_by_request")
        self.assertEqual(result["outcome"], "stop_requested")
        self.assertEqual(handoff_calls, [])
        self.assertEqual(result["worker_coordination"]["outcome"], "dispatch_stopped")

    def test_busy_promotion_lease_defers_without_model_attempt_or_handoff(self):
        from sira.runtime import run_unified_improvement_cycle

        lease = PromotionLease(self.root)
        self.assertTrue(lease.acquire("other-task"))
        handoff_calls = []
        attempt_records = []
        try:
            result = run_unified_improvement_cycle(
                self.root,
                41,
                target_selector=self._selector,
                evidence_builder=self._evidence,
                opportunity_researcher=self._research,
                opportunity_verifier=self._verify,
                opportunity_handoff_runner=lambda *_args: handoff_calls.append(True) or {},
                metered_budget_checker=self._budget,
                metered_attempt_recorder=lambda *_args, **_kwargs: attempt_records.append(True) or {"recorded": True},
            )
        finally:
            self.assertTrue(lease.release("other-task"))

        self.assertEqual(result["status"], "deferred_resource_budget")
        self.assertEqual(result["outcome"], "promotion_lease_busy")
        self.assertGreaterEqual(result["retry_after_seconds"], 1)
        self.assertEqual(handoff_calls, [])
        self.assertEqual(attempt_records, [])

    def test_autonomous_loop_recovers_orphaned_multi_worker_tasks_before_new_cycles(self):
        from sira.runtime import run_autonomous_loop

        worker_store = WorkerTaskStore(self.root)
        task = worker_store.create(self._target(), roles=("research", "verification"))
        worker_store.update(task["task_id"], status="running")

        def one_cycle(root, generation, keep_runtime_on=False):
            current, _ = self.store._load_raw()
            self.store.save({
                "desired_state": "off",
                "worker_state": "stopping",
                "pid": os.getpid(),
                "started_at": current.get("started_at"),
                "heartbeat_at": current.get("heartbeat_at"),
                "generation": generation,
            })
            return {"status": "completed", "outcome": "noop", "resource_usage": {}}

        report = run_autonomous_loop(self.root, 41, cycle_interval=0, cycle_runner=one_cycle)

        self.assertEqual(report["status"], "stopped")
        self.assertEqual(worker_store.load(task["task_id"])["status"], "abandoned_reselect")
        self.assertEqual(report["multi_worker_recovery"]["recovered_count"], 1)
        self.assertEqual(report["multi_worker_recovery"]["task_ids"], [task["task_id"]])

    def test_stale_promotion_lease_from_dead_worker_is_recovered(self):
        import json
        from sira.worker_coordination import recover_stale_promotion_lease

        lease_path = self.root / "runtime" / "promotion_worker_lease.json"
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(json.dumps({
            "schema_version": 1,
            "kind": "promotion_worker_lease",
            "owner_id": "dead-task",
            "owner_pid": 99999999,
            "created_at": "2026-09-18T00:00:00+00:00",
        }), encoding="utf-8")

        report = recover_stale_promotion_lease(self.root)

        self.assertTrue(report["recovered"])
        self.assertEqual(report["reason"], "dead_owner")
        self.assertFalse(lease_path.exists())



if __name__ == "__main__":
    unittest.main()
