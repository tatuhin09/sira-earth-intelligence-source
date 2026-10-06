"""Durable user goals are researched with explicit public scope and no model."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.access_runtime import target_identity
from sira.autonomous_targeting import select_autonomous_target
from sira.learning_goals import LearningGoalStore
from sira.learning_goal_runtime import run_learning_goal_target
from sira.runtime import RuntimeStateStore, run_unified_improvement_cycle
from sira.runtime_soak import _cycle_summary


class LearningGoalRuntimeV072Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.goals = LearningGoalStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def select(self, memory=()):
        return select_autonomous_target(
            self.root, now_epoch=2_000_000_000.0,
            memory_candidates_fn=lambda *_: list(memory),
            opportunity_discovery_fn=lambda *_: {"opportunities": [], "suppressed_cooldown_count": 0},
            knowledge_candidates_fn=lambda *_: [],
        )

    def test_owner_goal_beats_background_code_and_cools_down_after_attempt(self):
        saved = self.goals.create("Learn about agents", priority="high")
        selection = self.select()
        target = selection["target"]
        self.assertEqual(target_identity(target), "learning_goal:" + saved["goal_id"])
        self.assertEqual(selection["learning_goal_candidate_count"], 1)
        self.assertEqual(target["target_kind"], "learning_goal")
        self.assertEqual(target["priority_class"], 1)
        self.assertEqual(target["public_research_allowed"], False)

        calls = []
        report = run_learning_goal_target(
            self.root, target, now_epoch=2_000_000_000.0,
            knowledge_searcher=lambda *_a, **_k: calls.append("network"),
            open_access_searcher=lambda *_a, **_k: calls.append("network"),
        )
        self.assertEqual(report["outcome"], "insufficient_local_knowledge")
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(calls, [])
        self.assertFalse(report["verified_synthesis_performed"])
        self.assertEqual(self.goals.get(saved["goal_id"])["last_outcome"], report["outcome"])
        self.assertEqual(self.select()["learning_goal_candidate_count"], 0)

    def test_critical_failure_precedes_owner_goal_and_paused_goal_is_ignored(self):
        goal = self.goals.create("Some topic", priority="high")
        failure = {
            "memory_id": "m_" + "3" * 32, "kind": "failure", "category": "code_test",
            "status": "observed", "capability": "test", "priority_score": 1,
        }
        self.assertEqual(self.select([failure])["target"]["target_kind"], "memory")
        self.goals.set_status(goal["goal_id"], "paused")
        self.assertIsNone(self.select()["target"])

    def test_public_research_discovers_sources_without_treating_them_as_verified(self):
        goal = self.goals.create("Distributed memory agents", public_research_allowed=True)
        seen = []

        def search(_root, query, *, max_results):
            seen.append((query, max_results))
            return {"results": [{"title": "A source", "url": "https://example.org/a"}],
                    "metrics": {"api_requests": 1}}

        report = run_learning_goal_target(
            self.root, self.select()["target"], now_epoch=2_000_000_000.0,
            knowledge_searcher=search, open_access_searcher=search,
        )
        self.assertEqual(len(seen), 2)
        self.assertEqual(report["api_requests"], 2)
        self.assertEqual(report["metered_model_requests"], 0)
        self.assertEqual(report["outcome"], "sources_discovered_needs_verification")
        self.assertTrue(report["verified_synthesis_required"])
        self.assertFalse(report["verified_synthesis_performed"])
        self.assertFalse(report["promotion_performed"])
        self.assertEqual(self.goals.get(goal["goal_id"])["last_outcome"], report["outcome"])
        artifact = Path(report["artifact"])
        self.assertTrue(artifact.is_file())
        self.assertFalse(json.loads(artifact.read_text(encoding="utf-8"))["verified_synthesis_performed"])
        self.assertEqual(self.select()["learning_goal_candidate_count"], 0)

    def test_revoked_public_permission_is_checked_against_current_goal(self):
        saved = self.goals.create("Local private topic", public_research_allowed=True)
        target = self.select()["target"]
        self.goals.set_policy(saved["goal_id"], public_research_allowed=False)
        calls = []
        report = run_learning_goal_target(
            self.root, target, now_epoch=2_000_000_000.0,
            knowledge_searcher=lambda *_a, **_k: calls.append("network"),
            open_access_searcher=lambda *_a, **_k: calls.append("network"),
        )
        self.assertEqual(calls, [])
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(report["outcome"], "insufficient_local_knowledge")

    def test_public_permission_revoked_after_first_provider_blocks_second(self):
        saved = self.goals.create("Research sources for learning", public_research_allowed=True)
        first = []
        def knowledge(_root, _query, *, max_results):
            first.append(max_results)
            self.goals.set_policy(saved["goal_id"], public_research_allowed=False)
            return {"results": [{"title": "First public source"}], "metrics": {"api_requests": 1}}
        report = run_learning_goal_target(
            self.root, self.select()["target"], now_epoch=2_000_000_000.0,
            knowledge_searcher=knowledge,
            open_access_searcher=lambda *_a, **_k: self.fail("Second provider must not receive the topic"),
        )
        self.assertEqual(first, [3])
        self.assertEqual(report["outcome"], "research_permission_revoked")
        self.assertEqual(report["api_requests"], 1)
        self.assertEqual(report["research"]["knowledge"]["results"][0]["title"], "First public source")
        self.assertFalse(report["verified_synthesis_performed"])

    def test_runtime_routes_goal_without_writer_and_retains_runtime_ownership(self):
        goal = self.goals.create("Topic")
        state = RuntimeStateStore(self.root)
        state.save({
            "desired_state": "on", "worker_state": "starting", "pid": os.getpid(),
            "started_at": "2026-09-21T00:00:00+00:00", "heartbeat_at": "2026-09-21T00:00:00+00:00",
            "generation": 61,
        })
        target = self.select()["target"]
        writer_calls = []
        selector = lambda *_: {"status": "selected", "selection_id": "ats_" + "1" * 32,
                               "target": target, "alternatives": []}
        result = run_unified_improvement_cycle(
            self.root, 61, target_selector=selector,
            opportunity_handoff_runner=lambda *_a, **_k: writer_calls.append(True),
        )
        self.assertEqual(writer_calls, [])
        self.assertEqual(result["target_kind"], "learning_goal")
        self.assertEqual(result["learning_goal_id"], goal["goal_id"])
        self.assertEqual(result["outcome"], "insufficient_local_knowledge")
        self.assertEqual(result["resource_usage"]["metered_model_requests"], 0)
        self.assertFalse(result["promotion_performed"])
        self.assertFalse(result["main_tree_modified"])
        self.assertEqual(state.status()["desired_state"], "off")

    def test_soak_summary_traces_goal_and_counts_free_requests(self):
        goal = self.goals.create("Research topic", public_research_allowed=True)
        target = self.select()["target"]
        state = RuntimeStateStore(self.root)
        state.save({
            "desired_state": "on", "worker_state": "starting", "pid": os.getpid(),
            "started_at": "2026-09-21T00:00:00+00:00", "heartbeat_at": "2026-09-21T00:00:00+00:00",
            "generation": 62,
        })
        search = lambda *_a, **_k: {"results": [{"title": "Source"}], "metrics": {"api_requests": 1}}
        runner = lambda root, selected: run_learning_goal_target(
            root, selected, knowledge_searcher=search, open_access_searcher=search,
        )
        cycle = run_unified_improvement_cycle(
            self.root, 62,
            target_selector=lambda *_: {"status": "selected", "selection_id": "ats_" + "2" * 32,
                                        "target": target, "alternatives": []},
            learning_goal_runner=runner,
        )
        summary = _cycle_summary(cycle, 1)
        self.assertEqual(summary["learning_goal_id"], goal["goal_id"])
        self.assertEqual(summary["resource_usage"]["api_requests"], 2)
        self.assertEqual(summary["resource_usage"]["metered_model_requests"], 0)


if __name__ == "__main__":
    unittest.main()
