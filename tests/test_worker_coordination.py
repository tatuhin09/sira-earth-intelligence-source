import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.worker_coordination import (
    MultiWorkerCoordinator,
    PromotionLease,
    WorkerContext,
    WorkerTaskStore,
    recover_orphaned_worker_tasks,
    run_multi_worker_check,
)


class MultiWorkerFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _target():
        return {
            "target_kind": "opportunity",
            "opportunity_id": "op_" + "a" * 32,
            "path": "src/sira/retrieval.py",
            "symbol": "load_evidence_run",
        }

    def test_readonly_workers_run_concurrently_in_distinct_isolated_workspaces(self):
        coordinator = MultiWorkerCoordinator(self.root, max_readonly_workers=2)
        barrier = threading.Barrier(2)
        contexts = {}

        def worker(ctx: WorkerContext, target):
            contexts[ctx.role] = ctx
            self.assertEqual(target["opportunity_id"], self._target()["opportunity_id"])
            barrier.wait(timeout=2)
            return {"status": "ok", "role": ctx.role, "api_requests": 0}

        report = coordinator.run_task(
            self._target(),
            research_worker=worker,
            verification_worker=worker,
        )

        self.assertEqual(report["status"], "completed_readonly")
        self.assertEqual(report["worker_states"]["research"], "completed")
        self.assertEqual(report["worker_states"]["verification"], "completed")
        self.assertNotEqual(contexts["research"].workspace, contexts["verification"].workspace)
        self.assertTrue(contexts["research"].workspace.is_dir())
        self.assertTrue(contexts["verification"].workspace.is_dir())
        self.assertTrue((contexts["research"].artifact_dir / "result.json").is_file())
        self.assertTrue((contexts["verification"].artifact_dir / "result.json").is_file())
        self.assertFalse(report["promotion_performed"])

    def test_code_worker_is_serialized_after_readonly_phase_and_budget_gate_can_defer_it(self):
        coordinator = MultiWorkerCoordinator(self.root)
        order = []

        def research(ctx, _target):
            order.append("research")
            return {"status": "ok"}

        def verify(ctx, _target):
            order.append("verification")
            return {"status": "ok"}

        code_calls = []

        def code(ctx, _target, inputs):
            code_calls.append((ctx, inputs))
            order.append("code")
            return {"status": "candidate_prepared", "metered_model_requests": 1}

        deferred = coordinator.run_task(
            self._target(),
            research_worker=research,
            verification_worker=verify,
            code_worker=code,
            budget_checker=lambda _root: {
                "allowed": False,
                "reason": "minimum_interval",
                "retry_after_seconds": 90,
            },
        )
        self.assertEqual(deferred["status"], "deferred_resource_budget")
        self.assertEqual(deferred["retry_after_seconds"], 90)
        self.assertEqual(code_calls, [])

        allowed = coordinator.run_task(
            self._target(),
            research_worker=research,
            verification_worker=verify,
            code_worker=code,
            budget_checker=lambda _root: {"allowed": True, "reason": "available", "retry_after_seconds": 0},
        )
        self.assertEqual(allowed["status"], "candidate_prepared")
        self.assertEqual(allowed["worker_states"]["code"], "completed")
        self.assertEqual(len(code_calls), 1)
        self.assertIn("research", code_calls[0][1])
        self.assertIn("verification", code_calls[0][1])
        self.assertGreater(order.index("code"), order.index("research"))
        self.assertGreater(order.index("code"), order.index("verification"))

    def test_promotion_lease_allows_only_one_owner(self):
        first = PromotionLease(self.root)
        second = PromotionLease(self.root)
        self.assertTrue(first.acquire("task-a"))
        self.assertFalse(second.acquire("task-b"))
        self.assertFalse(second.release("task-b"))
        self.assertTrue(first.release("task-a"))
        self.assertTrue(second.acquire("task-b"))
        self.assertTrue(second.release("task-b"))

    def test_recovery_marks_running_task_abandoned_without_deleting_artifacts(self):
        store = WorkerTaskStore(self.root)
        task = store.create(self._target(), roles=("research", "verification"))
        task_root = Path(task["task_root"])
        marker = task_root / "artifacts" / "research" / "partial.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("keep", encoding="utf-8")
        store.update(task["task_id"], status="running")

        recovered = recover_orphaned_worker_tasks(self.root)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["task_id"], task["task_id"])
        self.assertEqual(recovered[0]["status"], "abandoned_reselect")
        self.assertTrue(marker.is_file())
        persisted = store.load(task["task_id"])
        self.assertEqual(persisted["status"], "abandoned_reselect")

    def test_offline_multi_worker_diagnostic_passes_without_external_calls(self):
        report = run_multi_worker_check(self.root)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(report["checks"].values()))
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(report["metered_model_requests"], 0)
        self.assertFalse(report["paid_spending"])
        self.assertTrue(Path(report["artifact"]).is_file())

    def test_cli_workers_check_runs_offline_diagnostic(self):
        from sira.cli import main

        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["--root", str(self.root), "self", "workers-check"])
        payload = json.loads(out.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["api_requests"], 0)
        self.assertEqual(payload["metered_model_requests"], 0)


if __name__ == "__main__":
    unittest.main()
