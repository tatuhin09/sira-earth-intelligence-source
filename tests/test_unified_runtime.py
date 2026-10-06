import json
import os
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.runtime import RuntimeStateStore


class UnifiedRuntimeCycleTests(unittest.TestCase):
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
            "generation": 21,
        })

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _opportunity_target():
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

    def test_opportunity_target_runs_evidence_research_and_writer_handoff(self):
        from sira.runtime import run_unified_improvement_cycle

        target = self._opportunity_target()
        calls = []

        def selector(root):
            calls.append(("select", Path(root)))
            return {
                "selection_id": "ats_" + "a" * 32,
                "status": "selected",
                "target": target,
            }

        def evidence_builder(root, opportunity_id):
            calls.append(("evidence", Path(root), opportunity_id))
            return {
                "evidence_id": "oe_" + "b" * 32,
                "assessment": {"decision": "research_ready"},
            }

        def researcher(root, evidence_id):
            calls.append(("research", Path(root), evidence_id))
            return {
                "research_id": "or_" + "c" * 32,
                "writer_handoff_allowed": True,
                "research_quality": {"decision": "writer_ready"},
            }

        def handoff(root, research_id):
            calls.append(("handoff", Path(root), research_id))
            return {
                "handoff_id": "ohf_" + "d" * 32,
                "status": "promoted",
                "outcome": "promotion_committed",
                "promotion_performed": True,
                "main_tree_modified": True,
                "promotion": {"promotion_id": "pr_" + "e" * 32},
            }

        result = run_unified_improvement_cycle(
            self.root,
            21,
            target_selector=selector,
            evidence_builder=evidence_builder,
            opportunity_researcher=researcher,
            opportunity_handoff_runner=handoff,
        )

        self.assertEqual([row[0] for row in calls], ["select", "evidence", "research", "handoff"])
        self.assertEqual(result["sira_runtime_stage"], "1.2")
        self.assertEqual(result["target_kind"], "opportunity")
        self.assertEqual(result["target_selection_id"], "ats_" + "a" * 32)
        self.assertEqual(result["opportunity_id"], target["opportunity_id"])
        self.assertEqual(result["evidence_id"], "oe_" + "b" * 32)
        self.assertEqual(result["research_id"], "or_" + "c" * 32)
        self.assertEqual(result["handoff_id"], "ohf_" + "d" * 32)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "promotion_committed")
        self.assertTrue(result["promotion_performed"])
        self.assertTrue(result["main_tree_modified"])
        persisted = json.loads((self.root / "runtime" / "last_cycle.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["cycle_id"], result["cycle_id"])
        self.assertEqual(persisted["target_kind"], "opportunity")
        self.assertEqual(persisted["handoff_id"], "ohf_" + "d" * 32)

    def test_stop_request_after_research_prevents_writer_handoff(self):
        from sira.runtime import run_unified_improvement_cycle

        target = self._opportunity_target()
        handoff_calls = []

        def selector(_root):
            return {"selection_id": "ats_" + "a" * 32, "status": "selected", "target": target}

        def evidence_builder(_root, _opportunity_id):
            return {"evidence_id": "oe_" + "b" * 32, "assessment": {"decision": "research_ready"}}

        def researcher(_root, _evidence_id):
            current, _ = self.store._load_raw()
            self.store.save({
                "desired_state": "off",
                "worker_state": "stopping",
                "pid": os.getpid(),
                "started_at": current.get("started_at"),
                "heartbeat_at": current.get("heartbeat_at"),
                "generation": 21,
            })
            return {
                "research_id": "or_" + "c" * 32,
                "writer_handoff_allowed": True,
                "research_quality": {"decision": "writer_ready"},
            }

        def handoff(*_args, **_kwargs):
            handoff_calls.append(True)
            raise AssertionError("writer handoff must not start after self off")

        result = run_unified_improvement_cycle(
            self.root,
            21,
            target_selector=selector,
            evidence_builder=evidence_builder,
            opportunity_researcher=researcher,
            opportunity_handoff_runner=handoff,
        )

        self.assertEqual(result["status"], "stopped_by_request")
        self.assertEqual(result["outcome"], "stop_requested")
        self.assertEqual(handoff_calls, [])
        self.assertFalse(result["promotion_performed"])

    def test_selected_memory_is_delegated_with_exact_memory_target(self):
        from sira.runtime import run_unified_improvement_cycle

        memory_id = "m_" + "f" * 32
        target = {
            "target_kind": "memory",
            "priority_class": 0,
            "priority_class_name": "failure_regression",
            "memory_id": memory_id,
            "priority_score": 20,
            "effective_priority_score": 20,
        }
        observed = {}

        def selector(_root):
            return {"selection_id": "ats_" + "a" * 32, "status": "selected", "target": target}

        def memory_cycle(root, generation, *, planner, keep_runtime_on=False, **_kwargs):
            observed["root"] = Path(root)
            observed["generation"] = generation
            observed["keep_runtime_on"] = keep_runtime_on
            plan = planner(Path(root))
            observed["plan"] = plan
            return {
                "schema_version": 1,
                "kind": "self_improvement_cycle",
                "cycle_id": "sc_" + "9" * 32,
                "generation": generation,
                "status": "completed",
                "outcome": "validated_existing_mitigation",
                "memory_id": plan["target"]["memory_id"],
                "promotion_performed": False,
                "main_tree_modified": False,
                "completed_at": "2026-09-18T00:00:01+00:00",
            }

        result = run_unified_improvement_cycle(
            self.root,
            21,
            target_selector=selector,
            memory_cycle_runner=memory_cycle,
            keep_runtime_on=True,
        )

        self.assertEqual(observed["root"], self.root.resolve())
        self.assertEqual(observed["generation"], 21)
        self.assertTrue(observed["keep_runtime_on"])
        self.assertEqual(observed["plan"]["target"]["memory_id"], memory_id)
        self.assertEqual(result["target_kind"], "memory")
        self.assertEqual(result["memory_id"], memory_id)
        self.assertEqual(result["target_selection_id"], "ats_" + "a" * 32)
        self.assertEqual(result["sira_runtime_stage"], "1.2")

    def test_idle_selection_keeps_worker_running_when_called_from_persistent_loop(self):
        from sira.runtime import run_unified_improvement_cycle

        def selector(_root):
            return {"selection_id": "ats_" + "a" * 32, "status": "idle_no_candidate", "target": None}

        result = run_unified_improvement_cycle(
            self.root,
            21,
            target_selector=selector,
            keep_runtime_on=True,
        )

        self.assertEqual(result["status"], "idle_no_candidate")
        self.assertEqual(result["outcome"], "no_candidate")
        status = self.store.status()
        self.assertEqual(status["desired_state"], "on")
        self.assertEqual(status["worker_state"], "running")


if __name__ == "__main__":
    unittest.main()

class UnifiedRuntimeReliabilityIntegrationTests(unittest.TestCase):
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
            "generation": 31,
        })

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _target():
        return {
            "target_kind": "opportunity",
            "priority_class": 2,
            "priority_class_name": "proactive_opportunity",
            "opportunity_id": "op_" + "7" * 32,
            "fingerprint": "8" * 64,
            "type": "complex_function",
            "path": "src/sira/retrieval.py",
            "symbol": "load_evidence_run",
            "priority_score": 401,
            "effective_priority_score": 401,
            "source_sha256": "9" * 64,
            "evidence": {"branch_points": 48, "loc": 71},
            "cooldown": {"eligible": True, "last_attempt_at": None, "remaining_seconds": 0},
            "lineage": {"eligible": True, "remaining_seconds": 0, "priority_penalty": 0},
        }

    def test_metered_budget_can_defer_writer_without_marking_cycle_failed(self):
        from sira.runtime import run_unified_improvement_cycle

        handoff_calls = []
        target = self._target()

        def selector(_root):
            return {"selection_id": "ats_" + "a" * 32, "status": "selected", "target": target}

        def evidence_builder(_root, _opportunity_id):
            return {"evidence_id": "oe_" + "b" * 32, "assessment": {"decision": "research_ready"}}

        def researcher(_root, _evidence_id):
            return {
                "research_id": "or_" + "c" * 32,
                "writer_handoff_allowed": True,
                "research_quality": {"decision": "writer_ready"},
                "api_requests": 2,
                "metered_model_requests": 0,
            }

        def handoff(*_args, **_kwargs):
            handoff_calls.append(True)
            raise AssertionError("writer must be deferred when metered budget is closed")

        def denied_budget(_root, *, now_epoch=None):
            return {"allowed": False, "reason": "rolling_budget_exhausted", "retry_after_seconds": 123}

        result = run_unified_improvement_cycle(
            self.root,
            31,
            target_selector=selector,
            evidence_builder=evidence_builder,
            opportunity_researcher=researcher,
            opportunity_handoff_runner=handoff,
            metered_budget_checker=denied_budget,
        )
        self.assertEqual(result["status"], "deferred_resource_budget")
        self.assertEqual(result["outcome"], "metered_retry_budget_exhausted")
        self.assertEqual(result["retry_after_seconds"], 123)
        self.assertEqual(handoff_calls, [])
        self.assertEqual(result["resource_usage"]["free_public_api_requests"], 2)
        self.assertEqual(result["resource_usage"]["metered_model_requests"], 0)

    def test_writer_attempt_is_recorded_before_handoff(self):
        from sira.runtime import run_unified_improvement_cycle

        target = self._target()
        events = []

        def selector(_root):
            return {"selection_id": "ats_" + "a" * 32, "status": "selected", "target": target}

        def evidence_builder(_root, _opportunity_id):
            return {"evidence_id": "oe_" + "b" * 32, "assessment": {"decision": "research_ready"}}

        def researcher(_root, _evidence_id):
            return {
                "research_id": "or_" + "c" * 32,
                "writer_handoff_allowed": True,
                "research_quality": {"decision": "writer_ready"},
                "api_requests": 1,
                "metered_model_requests": 0,
            }

        def allow(_root, *, now_epoch=None):
            events.append("budget_checked")
            return {"allowed": True, "reason": "available", "retry_after_seconds": 0}

        def record(_root, *, now_epoch=None):
            events.append("metered_recorded")
            return {"recorded": True}

        def handoff(_root, _research_id):
            events.append("handoff")
            return {
                "handoff_id": "ohf_" + "d" * 32,
                "status": "writer_error",
                "outcome": "http_503",
                "promotion_performed": False,
                "main_tree_modified": False,
                "writer_report": {"api_requests": 1, "attempt_count": 1},
            }

        result = run_unified_improvement_cycle(
            self.root,
            31,
            target_selector=selector,
            evidence_builder=evidence_builder,
            opportunity_researcher=researcher,
            opportunity_handoff_runner=handoff,
            metered_budget_checker=allow,
            metered_attempt_recorder=record,
        )
        self.assertEqual(events, ["budget_checked", "metered_recorded", "handoff"])
        self.assertEqual(result["resource_usage"]["free_public_api_requests"], 1)
        self.assertEqual(result["resource_usage"]["metered_model_requests"], 1)
        self.assertEqual(result["outcome"], "http_503")

    def test_loop_recovers_interrupted_cycle_then_keeps_running_after_transient_failure(self):
        from sira.runtime import run_autonomous_loop
        from sira.runtime_reliability import ActiveCycleJournal

        ActiveCycleJournal(self.root).start(
            generation=30,
            cycle_id="sc_" + "f" * 32,
            phase="writer_handoff",
        )
        calls = []

        def cycle_runner(_root, generation, *, keep_runtime_on=False):
            calls.append((generation, keep_runtime_on))
            current, _ = self.store._load_raw()
            self.store.save({
                "desired_state": "off",
                "worker_state": "stopping",
                "pid": os.getpid(),
                "started_at": current.get("started_at"),
                "heartbeat_at": current.get("heartbeat_at"),
                "generation": generation,
            })
            return {"status": "completed", "outcome": "http_503", "resource_usage": {"metered_model_requests": 1}}

        sleeps = []
        result = run_autonomous_loop(
            self.root,
            31,
            cycle_interval=0,
            cycle_runner=cycle_runner,
            sleep_fn=lambda seconds: sleeps.append(seconds),
        )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["recovery"]["status"], "recovered_interrupted_cycle")
        self.assertEqual(len(calls), 1)
        self.assertTrue((self.root / "runtime" / "last_recovery.json").is_file())
