"""An independently corroborated title hint may guide one paced gap search."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_goals import LearningGoalStore
from sira.learning_goal_runtime import run_learning_goal_target


class HintedGapCadenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create("Network defense", public_research_allowed=True)
        self.goal_id = self.goal["goal_id"]
        self.first = "Network security controls"
        self.second = "Incident response"
        self.store.set_plan(self.goal_id, [self.first, self.second])
        self.start = 2_000_000_000.0
        self.hint = {"term": "segmentation",
                     "source_hosts": ["doaj.org", "en.wikipedia.org"],
                     "source_urls": ["https://doaj.org/article/123",
                                     "https://en.wikipedia.org/wiki/Network_segmentation"]}

    def completed_steps(self, *, with_hint):
        self.store.record_attempt(self.goal_id, "sources_discovered_needs_verification",
                                  at_epoch=self.start, study_step_index=0,
                                  research_policy_version=2, research_focus=self.first,
                                  followup_hint=self.hint if with_hint else None)
        self.store.record_attempt(self.goal_id, "research_sources_insufficient",
                                  at_epoch=self.start + 30 * 60, study_step_index=1,
                                  research_policy_version=2, research_focus=self.second)

    def test_one_hint_search_runs_before_daily_retry_then_stops(self):
        self.completed_steps(with_hint=True)
        self.assertEqual(self.store.get(self.goal_id)["study_next_index"], 0)
        self.assertEqual(self.store.candidates(now_epoch=self.start + 60 * 60 - 1), [])
        self.assertEqual([x["goal_id"] for x in self.store.candidates(
            now_epoch=self.start + 60 * 60)], [self.goal_id])

        queries = []

        def search(_root, query, *, max_results):
            queries.append(query)
            return {"status": "failed", "results": [], "provider_failures": [],
                    "metrics": {"api_requests": 0}}

        result = run_learning_goal_target(
            self.root, {"target_kind": "learning_goal", "learning_goal_id": self.goal_id,
                        "topic": self.goal["topic"]}, now_epoch=self.start + 60 * 60,
            knowledge_searcher=search, open_access_searcher=search,
        )
        self.assertEqual(result.get("research_query"), self.first)
        self.assertEqual(result.get("search_query"), self.first + " segmentation")
        self.assertEqual(result.get("query_strategy"), "unverified_title_followup")
        self.assertEqual(queries, [self.first + " segmentation"] * 2)
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(self.store.candidates(now_epoch=self.start + 90 * 60), [])

    def test_no_independent_hint_keeps_daily_retry(self):
        self.completed_steps(with_hint=False)
        self.assertEqual(self.store.candidates(now_epoch=self.start + 60 * 60), [])
        self.assertEqual([x["goal_id"] for x in self.store.candidates(
            now_epoch=self.start + 24 * 60 * 60 + 30 * 60)], [self.goal_id])


if __name__ == "__main__":
    unittest.main()
