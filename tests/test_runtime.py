import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.cli import main
from sira.runtime import RuntimeStateStore


class RuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_state_is_fail_closed_and_status_is_json(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["--root", str(self.root), "self", "status"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["kind"], "self_status")
        self.assertEqual(payload["desired_state"], "off")
        self.assertEqual(payload["effective_state"], "stopped")
        self.assertFalse(payload["worker_alive"])
        self.assertIsNone(payload["pid"])
        self.assertEqual(payload["state_health"], "missing_default")
        self.assertFalse((self.root / "runtime" / "self_state.json").exists())

    def test_store_round_trips_atomic_state_and_reports_live_pid(self):
        store = RuntimeStateStore(self.root)
        store.save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": os.getpid(),
            "started_at": "2026-09-17T10:00:00+00:00",
            "heartbeat_at": "2026-09-17T10:00:05+00:00",
            "generation": 3,
        })
        status = store.status()
        self.assertEqual(status["desired_state"], "on")
        self.assertEqual(status["effective_state"], "running")
        self.assertTrue(status["worker_alive"])
        self.assertEqual(status["pid"], os.getpid())
        self.assertEqual(status["generation"], 3)
        self.assertEqual(status["state_health"], "ok")

    def test_stale_pid_and_corrupt_state_fail_closed(self):
        store = RuntimeStateStore(self.root)
        store.save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": 99999999,
            "started_at": "2026-09-17T10:00:00+00:00",
            "heartbeat_at": "2026-09-17T10:00:05+00:00",
            "generation": 1,
        })
        stale = store.status()
        self.assertEqual(stale["desired_state"], "on")
        self.assertEqual(stale["effective_state"], "stopped")
        self.assertFalse(stale["worker_alive"])
        self.assertEqual(stale["state_health"], "stale_worker")

        store.path.write_text("{not-json", encoding="utf-8")
        corrupt = store.status()
        self.assertEqual(corrupt["desired_state"], "off")
        self.assertEqual(corrupt["effective_state"], "stopped")
        self.assertEqual(corrupt["state_health"], "corrupt_fail_closed")
        self.assertIsNone(corrupt["pid"])


if __name__ == "__main__":
    unittest.main()

class _FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid


class _FakeLauncher:
    def __init__(self, pid: int = 424242):
        self.pid = pid
        self.calls = []

    def launch(self, root: Path, generation: int):
        self.calls.append((Path(root), generation))
        return _FakeProcess(self.pid)


class AutonomousRuntimeStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_records_generation_and_launches_one_background_probe(self):
        from sira.runtime import AutonomousRuntime

        launcher = _FakeLauncher()
        result = AutonomousRuntime(self.root, launcher=launcher).start()
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["sira_runtime_stage"], "1.2")
        self.assertEqual(result["generation"], 1)
        self.assertEqual(result["pid"], 424242)
        self.assertEqual(result["mode"], "persistent_improvement_loop")
        self.assertEqual(launcher.calls, [(self.root.resolve(), 1)])

        raw = json.loads((self.root / "runtime" / "self_state.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["desired_state"], "on")
        self.assertEqual(raw["worker_state"], "starting")
        self.assertEqual(raw["pid"], 424242)

    def test_start_is_idempotent_when_worker_is_already_alive(self):
        from sira.runtime import AutonomousRuntime

        store = RuntimeStateStore(self.root)
        store.save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": os.getpid(),
            "started_at": "2026-09-17T10:00:00+00:00",
            "heartbeat_at": "2026-09-17T10:00:05+00:00",
            "generation": 7,
        })
        launcher = _FakeLauncher()
        result = AutonomousRuntime(self.root, launcher=launcher).start()
        self.assertEqual(result["status"], "already_running")
        self.assertEqual(result["generation"], 7)
        self.assertEqual(launcher.calls, [])

    def test_heartbeat_probe_claims_generation_updates_heartbeat_and_stops(self):
        from sira.runtime import run_heartbeat_probe

        store = RuntimeStateStore(self.root)
        store.save({
            "desired_state": "on",
            "worker_state": "starting",
            "pid": os.getpid(),
            "started_at": "2026-09-17T10:00:00+00:00",
            "heartbeat_at": "2026-09-17T10:00:00+00:00",
            "generation": 3,
        })
        sleeps = []
        result = run_heartbeat_probe(
            self.root,
            3,
            heartbeat_count=3,
            heartbeat_interval=0.01,
            sleep_fn=lambda seconds: sleeps.append(seconds),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["heartbeats"], 3)
        self.assertEqual(sleeps, [0.01, 0.01])
        status = store.status()
        self.assertEqual(status["desired_state"], "off")
        self.assertEqual(status["effective_state"], "stopped")
        self.assertEqual(status["worker_state"], "stopped")
        self.assertEqual(status["generation"], 3)

    def test_probe_refuses_stale_generation(self):
        from sira.runtime import run_heartbeat_probe

        store = RuntimeStateStore(self.root)
        store.save({
            "desired_state": "on",
            "worker_state": "starting",
            "pid": os.getpid(),
            "started_at": "2026-09-17T10:00:00+00:00",
            "heartbeat_at": "2026-09-17T10:00:00+00:00",
            "generation": 5,
        })
        result = run_heartbeat_probe(self.root, 4, heartbeat_count=1, sleep_fn=lambda _: None)
        self.assertEqual(result["status"], "stale_generation")
        self.assertEqual(store.status()["generation"], 5)

    def test_cli_self_on_uses_runtime_controller_and_returns_json(self):
        from unittest.mock import patch

        fake = type("FakeRuntime", (), {"start": lambda self: {
            "status": "started",
            "sira_runtime_stage": "1.2",
            "generation": 1,
            "pid": 123,
            "mode": "persistent_improvement_loop",
        }})()
        out = io.StringIO()
        with patch("sira.cli.AutonomousRuntime", return_value=fake):
            with redirect_stdout(out):
                rc = main(["--root", str(self.root), "self", "on"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "started")
        self.assertEqual(payload["mode"], "persistent_improvement_loop")

class AutonomousImprovementCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _armed_store(self, generation=1):
        store = RuntimeStateStore(self.root)
        store.save({
            "desired_state": "on",
            "worker_state": "starting",
            "pid": os.getpid(),
            "started_at": "2026-09-17T10:00:00+00:00",
            "heartbeat_at": "2026-09-17T10:00:00+00:00",
            "generation": generation,
        })
        return store

    def test_one_cycle_runs_plan_research_experiment_and_persists_summary(self):
        from sira.runtime import run_one_improvement_cycle

        store = self._armed_store(generation=2)
        calls = []

        def planner(root):
            calls.append(("plan", Path(root)))
            return {"plan_id": "ip_" + "a" * 32, "target": {"memory_id": "m_" + "b" * 32}}

        def researcher(root, memory_id):
            calls.append(("research", memory_id))
            return {"hypothesis_id": "ih_" + "c" * 32, "memory_id": memory_id, "repeat_blocked": False}

        def experimenter(root, hypothesis_id):
            calls.append(("experiment", hypothesis_id))
            return {
                "experiment_id": "ix_" + "d" * 32,
                "memory_id": "m_" + "b" * 32,
                "outcome": "validated_existing_mitigation",
                "promotion_performed": False,
                "main_tree_modified": False,
            }

        result = run_one_improvement_cycle(
            self.root,
            2,
            planner=planner,
            researcher=researcher,
            experimenter=experimenter,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "validated_existing_mitigation")
        self.assertFalse(result["promotion_performed"])
        self.assertEqual([row[0] for row in calls], ["plan", "research", "experiment"])
        cycle_path = self.root / "runtime" / "last_cycle.json"
        persisted = json.loads(cycle_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["cycle_id"], result["cycle_id"])
        self.assertEqual(persisted["experiment_id"], "ix_" + "d" * 32)
        status = store.status()
        self.assertEqual(status["desired_state"], "off")
        self.assertEqual(status["worker_state"], "stopped")
        self.assertEqual(status["last_cycle"]["status"], "completed")
        self.assertEqual(status["last_cycle"]["outcome"], "validated_existing_mitigation")

    def test_one_cycle_with_no_candidate_stops_cleanly_as_idle(self):
        from sira.runtime import run_one_improvement_cycle

        store = self._armed_store(generation=4)

        def planner(_root):
            raise ValueError("No observed or rejected failure memory is available for improvement")

        result = run_one_improvement_cycle(self.root, 4, planner=planner)
        self.assertEqual(result["status"], "idle_no_candidate")
        self.assertIsNone(result["memory_id"])
        status = store.status()
        self.assertEqual(status["desired_state"], "off")
        self.assertEqual(status["worker_state"], "stopped")
        self.assertEqual(status["last_cycle"]["status"], "idle_no_candidate")

    def test_runtime_start_reports_persistent_improvement_loop_mode(self):
        from sira.runtime import AutonomousRuntime

        launcher = _FakeLauncher()
        result = AutonomousRuntime(self.root, launcher=launcher).start()
        self.assertEqual(result["sira_runtime_stage"], "1.2")
        self.assertEqual(result["mode"], "persistent_improvement_loop")

class AutonomousPersistentLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _armed_store(self, generation=1, worker_state="starting"):
        store = RuntimeStateStore(self.root)
        store.save({
            "desired_state": "on",
            "worker_state": worker_state,
            "pid": os.getpid(),
            "started_at": "2026-09-17T10:00:00+00:00",
            "heartbeat_at": "2026-09-17T10:00:00+00:00",
            "generation": generation,
        })
        return store

    def test_stop_requests_graceful_shutdown_for_live_worker(self):
        from sira.runtime import AutonomousRuntime

        store = self._armed_store(generation=8, worker_state="running")
        result = AutonomousRuntime(self.root).stop()
        self.assertEqual(result["status"], "stop_requested")
        self.assertEqual(result["sira_runtime_stage"], "1.2")
        self.assertEqual(result["generation"], 8)
        raw, health = store._load_raw()
        self.assertEqual(health, "ok")
        self.assertEqual(raw["desired_state"], "off")
        self.assertEqual(raw["worker_state"], "stopping")
        self.assertEqual(raw["pid"], os.getpid())

    def test_stop_is_idempotent_when_runtime_is_already_stopped(self):
        from sira.runtime import AutonomousRuntime

        result = AutonomousRuntime(self.root).stop()
        self.assertEqual(result["status"], "already_stopped")
        self.assertEqual(result["sira_runtime_stage"], "1.2")
        self.assertEqual(RuntimeStateStore(self.root).status()["desired_state"], "off")

    def test_persistent_loop_runs_multiple_cycles_until_stop_requested(self):
        from sira.runtime import run_autonomous_loop

        store = self._armed_store(generation=3)
        calls = []

        def cycle_runner(root, generation, *, keep_runtime_on=False):
            calls.append((Path(root), generation, keep_runtime_on))
            if len(calls) == 2:
                current, _ = store._load_raw()
                store.save({
                    "desired_state": "off",
                    "worker_state": "stopping",
                    "pid": os.getpid(),
                    "started_at": current.get("started_at"),
                    "heartbeat_at": current.get("heartbeat_at"),
                    "generation": generation,
                })
            return {"status": "completed", "outcome": "validated_existing_mitigation"}

        result = run_autonomous_loop(
            self.root,
            3,
            cycle_interval=0,
            sleep_fn=lambda _: None,
            cycle_runner=cycle_runner,
        )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["cycles_completed"], 2)
        self.assertEqual([row[2] for row in calls], [True, True])
        status = store.status()
        self.assertEqual(status["desired_state"], "off")
        self.assertEqual(status["worker_state"], "stopped")
        self.assertFalse(status["worker_alive"])

    def test_start_reports_persistent_loop_and_recovers_stale_generation(self):
        from sira.runtime import AutonomousRuntime

        store = RuntimeStateStore(self.root)
        store.save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": 99999999,
            "started_at": "2026-09-17T10:00:00+00:00",
            "heartbeat_at": "2026-09-17T10:00:00+00:00",
            "generation": 5,
        })
        launcher = _FakeLauncher()
        result = AutonomousRuntime(self.root, launcher=launcher).start()
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["sira_runtime_stage"], "1.2")
        self.assertEqual(result["mode"], "persistent_improvement_loop")
        self.assertEqual(result["generation"], 6)
        self.assertTrue(result["recovered_from_stale"])

    def test_cli_self_off_uses_runtime_controller(self):
        from unittest.mock import patch

        fake = type("FakeRuntime", (), {"stop": lambda self: {
            "status": "stop_requested",
            "sira_runtime_stage": "1.2",
            "generation": 2,
            "pid": 123,
            "mode": "persistent_improvement_loop",
        }})()
        out = io.StringIO()
        with patch("sira.cli.AutonomousRuntime", return_value=fake):
            with redirect_stdout(out):
                rc = main(["--root", str(self.root), "self", "off"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "stop_requested")
        self.assertEqual(payload["mode"], "persistent_improvement_loop")

class AutonomousPromotionCycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = RuntimeStateStore(self.root)
        self.store.save({
            "desired_state": "on",
            "worker_state": "starting",
            "pid": os.getpid(),
            "started_at": "2026-09-17T10:00:00+00:00",
            "heartbeat_at": "2026-09-17T10:00:00+00:00",
            "generation": 11,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_cycle_routes_structured_candidate_edits_through_autonomous_promotion(self):
        from sira.runtime import run_one_improvement_cycle

        calls = []

        def planner(_root):
            return {"plan_id": "ip_" + "1" * 32, "target": {"memory_id": "m_" + "2" * 32}}

        def researcher(_root, memory_id):
            return {
                "hypothesis_id": "ih_" + "3" * 32,
                "memory_id": memory_id,
                "repeat_blocked": False,
                "candidate_text_edits": {"src/sira/feature.py": "VALUE = 2\n"},
            }

        def experimenter(*_args, **_kwargs):
            raise AssertionError("legacy experimenter must not run when structured edits exist")

        def promoter(root, hypothesis):
            calls.append((Path(root), hypothesis["hypothesis_id"]))
            return {
                "status": "promoted",
                "outcome": "promotion_committed",
                "promotion_performed": True,
                "main_tree_modified": True,
                "promotion": {"promotion_id": "pr_" + "4" * 32},
            }

        result = run_one_improvement_cycle(
            self.root,
            11,
            planner=planner,
            researcher=researcher,
            experimenter=experimenter,
            autonomous_promoter=promoter,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "promotion_committed")
        self.assertTrue(result["promotion_performed"])
        self.assertTrue(result["main_tree_modified"])
        self.assertEqual(result["promotion_id"], "pr_" + "4" * 32)
        self.assertEqual(calls, [(self.root.resolve(), "ih_" + "3" * 32)])

    def test_cycle_invokes_code_writer_when_research_has_no_structured_edits(self):
        from sira.runtime import run_one_improvement_cycle

        writer = object()
        calls = []

        def planner(_root):
            return {"plan_id": "ip_" + "1" * 32, "target": {"memory_id": "m_" + "2" * 32}}

        def researcher(_root, memory_id):
            return {
                "hypothesis_id": "ih_" + "3" * 32,
                "memory_id": memory_id,
                "repeat_blocked": False,
                "benchmark_suite": "papers",
            }

        def writer_factory(root):
            calls.append(("writer_factory", Path(root)))
            return writer

        def promoter(root, hypothesis, *, writer=None):
            calls.append(("promoter", Path(root), hypothesis["hypothesis_id"], writer))
            return {
                "status": "promoted",
                "outcome": "promotion_committed",
                "promotion_performed": True,
                "main_tree_modified": True,
                "writer_report": {"api_requests": 1, "model": {"provider": "fixture"}},
                "promotion": {"promotion_id": "pr_" + "4" * 32},
            }

        def experimenter(*_args, **_kwargs):
            raise AssertionError("legacy experiment must not run after a generated candidate is promoted")

        result = run_one_improvement_cycle(
            self.root,
            11,
            planner=planner,
            researcher=researcher,
            experimenter=experimenter,
            autonomous_promoter=promoter,
            code_writer_factory=writer_factory,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "promotion_committed")
        self.assertTrue(result["code_writer_attempted"])
        self.assertEqual(result["code_writer_status"], "promotion_attempted")
        self.assertEqual(result["code_writer"]["api_requests"], 1)
        self.assertTrue(result["promotion_performed"])
        self.assertEqual(calls[0], ("writer_factory", self.root.resolve()))
        self.assertEqual(calls[1][0:3], ("promoter", self.root.resolve(), "ih_" + "3" * 32))
        self.assertIs(calls[1][3], writer)

    def test_writer_failure_falls_back_to_non_mutating_regression_experiment(self):
        from sira.runtime import run_one_improvement_cycle

        def planner(_root):
            return {"plan_id": "ip_" + "1" * 32, "target": {"memory_id": "m_" + "2" * 32}}

        def researcher(_root, memory_id):
            return {
                "hypothesis_id": "ih_" + "3" * 32,
                "memory_id": memory_id,
                "repeat_blocked": False,
                "benchmark_suite": "papers",
            }

        def writer_factory(_root):
            raise ValueError("GEMINI_API_KEY missing; run: python sira.py setup-model-key")

        def experimenter(_root, hypothesis_id):
            self.assertEqual(hypothesis_id, "ih_" + "3" * 32)
            return {
                "experiment_id": "ix_" + "5" * 32,
                "memory_id": "m_" + "2" * 32,
                "outcome": "validated_existing_mitigation",
                "promotion_performed": False,
                "main_tree_modified": False,
            }

        result = run_one_improvement_cycle(
            self.root,
            11,
            planner=planner,
            researcher=researcher,
            experimenter=experimenter,
            code_writer_factory=writer_factory,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "validated_existing_mitigation")
        self.assertTrue(result["code_writer_attempted"])
        self.assertEqual(result["code_writer_status"], "fallback_experiment")
        self.assertEqual(result["code_writer_error"]["type"], "ValueError")
        self.assertFalse(result["promotion_performed"])
        self.assertFalse(result["main_tree_modified"])
