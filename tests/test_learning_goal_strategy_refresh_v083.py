"""An improved research policy may retry an incomplete goal exactly once."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_goals import GOAL_RETRY_SECONDS, LearningGoalStore
from sira.learning_goal_runtime import run_learning_goal_target


class LearningGoalStrategyRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.now = 2_000_000_000.0

    def test_legacy_incomplete_public_goal_retries_once_under_new_policy(self):
        goal = self.store.create("Python software testing", public_research_allowed=True)
        self.store.record_attempt(goal["goal_id"], "sources_discovered_needs_verification",
                                  at_epoch=self.now - 100)
        self.assertEqual(len(self.store.candidates(now_epoch=self.now)), 1)

        target = {"target_kind": "learning_goal", "learning_goal_id": goal["goal_id"],
                  "topic": goal["topic"]}
        search = lambda *_a, **_k: {"results": [], "metrics": {"api_requests": 0}}
        report = run_learning_goal_target(
            self.root, target, now_epoch=self.now,
            knowledge_searcher=search, open_access_searcher=search,
        )
        self.assertEqual(report["outcome"], "research_sources_insufficient")
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(report["metered_model_requests"], 0)
        self.assertEqual(report["research_policy_version"], 2)
        self.assertNotIn(goal["goal_id"],
                         {row["goal_id"] for row in self.store.candidates(now_epoch=self.now + 1)})
        self.assertEqual(self.store.get(goal["goal_id"])["last_research_policy_version"], 2)
        self.assertEqual(len(self.store.candidates(now_epoch=self.now + GOAL_RETRY_SECONDS)), 1)

    def test_recorded_policy_marker_cannot_be_downgraded(self):
        goal = self.store.create("Durable policy", public_research_allowed=True)
        self.store.record_attempt(goal["goal_id"], "research_sources_insufficient",
                                  at_epoch=self.now, research_policy_version=2)
        self.store.record_attempt(goal["goal_id"], "research_sources_insufficient",
                                  at_epoch=self.now + 1, research_policy_version=1)
        self.assertEqual(self.store.get(goal["goal_id"])["last_research_policy_version"], 2)
        self.assertEqual(self.store.candidates(now_epoch=self.now + 2), [])

    def test_legacy_private_success_paused_and_crash_markers_do_not_bypass_cooldown(self):
        for topic, public, outcome, paused in (
            ("private", False, "sources_discovered_needs_verification", False),
            ("verified", True, "verified_knowledge_recorded", False),
            ("crashed", True, "research_started", False),
            ("paused", True, "sources_discovered_needs_verification", True),
        ):
            goal = self.store.create(topic, public_research_allowed=public)
            self.store.record_attempt(goal["goal_id"], outcome, at_epoch=self.now - 100)
            if paused:
                self.store.set_status(goal["goal_id"], "paused")
        self.assertEqual(self.store.candidates(now_epoch=self.now), [])

    def test_invalid_research_policy_marker_fails_closed(self):
        import json
        goal = self.store.create("Policy marker", public_research_allowed=True)
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        payload["goals"][goal["goal_id"]]["last_research_policy_version"] = "new"
        self.store.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.candidates(now_epoch=self.now)


if __name__ == "__main__":
    unittest.main()
