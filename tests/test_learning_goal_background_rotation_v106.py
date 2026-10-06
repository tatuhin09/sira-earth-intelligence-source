"""Public learning goals share a paced lane for fresh study steps."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_goals import LearningGoalStore
from sira.learning_goal_runtime import run_learning_goal_target


class BackgroundRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.first = self.store.create("Network defense", priority="high",
                                       public_research_allowed=True)
        self.second = self.store.create("Secure software", priority="high",
                                        public_research_allowed=True)
        self.store.set_plan(self.first["goal_id"], ["Network segmentation", "Traffic logging"])
        self.store.set_plan(self.second["goal_id"], ["Input validation", "Secure deployment"])
        self.start = 2_000_000_000.0
        self.store.record_attempt(self.first["goal_id"], "research_sources_insufficient",
                                  at_epoch=self.start, study_step_index=0,
                                  research_policy_version=2)
        self.store.record_attempt(self.second["goal_id"], "research_sources_insufficient",
                                  at_epoch=self.start + 10, study_step_index=0,
                                  research_policy_version=2)

    def eligible(self, when):
        return [goal["goal_id"] for goal in self.store.candidates(now_epoch=when)]

    @staticmethod
    def offline_search(_root, _query, *, max_results):
        return {"status": "failed", "results": [], "provider_failures": [],
                "metrics": {"api_requests": 0}}

    def study(self, goal, when):
        return run_learning_goal_target(
            self.root, {"target_kind": "learning_goal", "learning_goal_id": goal["goal_id"],
                        "topic": goal["topic"]}, now_epoch=when,
            knowledge_searcher=self.offline_search,
            open_access_searcher=self.offline_search,
        )

    def test_new_steps_share_a_global_pause_and_rotate_among_goals(self):
        self.assertEqual(self.eligible(self.start + 30 * 60 + 9), [])
        self.assertEqual(self.eligible(self.start + 30 * 60 + 10),
                         [self.first["goal_id"], self.second["goal_id"]])

        first_time = self.start + 30 * 60 + 10
        self.assertEqual(self.study(self.first, first_time).get("research_query"),
                         "Traffic logging")
        self.assertEqual(self.eligible(first_time + 1), [])
        self.assertEqual(self.eligible(first_time + 30 * 60)[0], self.second["goal_id"])
        self.assertEqual(self.study(self.second, first_time + 30 * 60).get("research_query"),
                         "Secure deployment")
        self.assertEqual(self.eligible(first_time + 60 * 60), [])

    def test_recent_unplanned_goal_also_paces_other_fresh_steps(self):
        other = self.store.create("New learning goal", public_research_allowed=True)
        self.store.record_attempt(other["goal_id"], "research_sources_insufficient",
                                  at_epoch=self.start + 30 * 60, research_policy_version=2)
        self.assertEqual(self.eligible(self.start + 30 * 60 + 10), [])
        self.assertEqual(self.eligible(self.start + 60 * 60),
                         [self.first["goal_id"], self.second["goal_id"]])


if __name__ == "__main__":
    unittest.main()
