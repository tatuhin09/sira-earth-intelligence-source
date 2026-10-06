"""A planned goal can study fresh subtopics without repeating a recent search."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_goals import LearningGoalStore
from sira.learning_goal_runtime import run_learning_goal_target


class DistinctStepCooldownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create("Cybersecurity defense", public_research_allowed=True)
        self.goal_id = self.goal["goal_id"]
        self.store.set_plan(self.goal_id, ["Risk assessment", "Access control", "Incident response"])
        self.target = {"target_kind": "learning_goal", "learning_goal_id": self.goal_id,
                       "topic": self.goal["topic"]}
        self.start = 2_000_000_000.0

    @staticmethod
    def offline_search(_root, _query, *, max_results):
        return {"status": "failed", "results": [], "provider_failures": [],
                "metrics": {"api_requests": 0}}

    def study(self, when):
        return run_learning_goal_target(
            self.root, self.target, now_epoch=when,
            knowledge_searcher=self.offline_search,
            open_access_searcher=self.offline_search,
        )

    def test_next_untried_step_runs_after_thirty_minutes_and_still_respects_daily_retries(self):
        first = self.study(self.start)
        self.assertEqual(first["research_query"], "Risk assessment")
        self.assertEqual(self.study(self.start + 30 * 60 - 1)["outcome"], "goal_cooldown")

        second = self.study(self.start + 30 * 60)
        self.assertEqual(second.get("research_query"), "Access control")
        third = self.study(self.start + 60 * 60)
        self.assertEqual(third.get("research_query"), "Incident response")
        self.assertEqual(self.study(self.start + 90 * 60)["outcome"], "goal_cooldown")
        self.assertEqual(self.store.get(self.goal_id)["study_next_index"], 0)
        self.assertEqual(self.study(self.start + 25 * 60 * 60)["research_query"],
                         "Risk assessment")

    def test_interrupted_new_step_does_not_repeatedly_restart(self):
        self.study(self.start)
        next_time = self.start + 30 * 60
        self.store.record_attempt(self.goal_id, "research_started", at_epoch=next_time,
                                  research_policy_version=2)
        self.assertEqual(self.study(next_time + 6 * 60 * 60)["outcome"], "goal_cooldown")
        self.assertEqual(self.study(next_time + 24 * 60 * 60)["research_query"],
                         "Access control")

    def test_paused_goal_does_not_become_eligible_for_fresh_step(self):
        self.study(self.start)
        self.store.set_status(self.goal_id, "paused")
        self.assertEqual(self.store.candidates(now_epoch=self.start + 6 * 60 * 60), [])
        self.assertEqual(self.study(self.start + 6 * 60 * 60)["outcome"], "goal_paused")


if __name__ == "__main__":
    unittest.main()
