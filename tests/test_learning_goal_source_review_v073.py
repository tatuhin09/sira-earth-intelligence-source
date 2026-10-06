"""Public search results are candidates, never verified knowledge."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_goal_source_review import review_discovered_sources
from sira.learning_goal_runtime import run_learning_goal_target
from sira.learning_goals import LearningGoalStore


class LearningGoalSourceReviewV073Tests(unittest.TestCase):
    def test_relevance_does_not_promote_broad_search_results_to_evidence(self):
        research = {
            "knowledge": {"results": [
                {"title": "Python (programming language)", "description": "A programming language", "url": "https://en.wikipedia.org/wiki/Python"},
                {"title": "Software testing", "description": "Testing software", "url": "https://en.wikipedia.org/wiki/Software_testing"},
            ]},
            "open_access": {"results": [
                {"title": "Underwater vehicle recovery", "abstract": "A docking hoop", "url": "https://doaj.org/article/1"},
                {"title": "Python software testing: methods", "abstract": "Methods for testing Python software", "url": "https://doaj.org/article/2"},
            ]},
        }
        report = review_discovered_sources("Python software testing", research)
        self.assertEqual(report["status"], "needs_claim_verification")
        self.assertEqual(report["source_count"], 4)
        self.assertEqual(report["distinct_host_count"], 2)
        self.assertEqual(report["high_overlap_count"], 1)
        self.assertEqual(report["candidates"][0]["title"], "Python software testing: methods")
        self.assertEqual(report["candidates"][0]["provider_kind"], "open_access")
        self.assertEqual(report["candidates"][0]["result_index"], 1)
        self.assertEqual(report["candidates"][0]["overlap"], "high")
        self.assertEqual(report["candidates"][-1]["overlap"], "none")
        self.assertFalse(report["verified_claims_recorded"])

    def test_same_host_and_duplicate_url_cannot_be_counted_as_independent(self):
        rows = [
            {"title": "Python software testing", "url": "https://docs.example.org/a"},
            {"title": "Python software testing", "url": "https://docs.example.org/a"},
            {"title": "Python software testing", "url": "https://docs.example.org/b"},
        ]
        report = review_discovered_sources("Python software testing", {"knowledge": {"results": rows}})
        self.assertEqual(report["source_count"], 2)
        self.assertEqual(report["distinct_host_count"], 1)
        self.assertEqual(report["high_overlap_host_count"], 1)
        self.assertFalse(report["verified_claims_recorded"])

    def test_incidental_terms_in_abstract_cannot_make_unrelated_title_high(self):
        titles = [
            "Network Reconstruction and Modelling Made Reproducible with moped",
            "Robust Design of Docking Hoop for Recovery of Autonomous Underwater Vehicle",
            "TomoPhantom, a software package to generate analytical phantoms",
        ]
        report = review_discovered_sources("Python software testing", {
            "open_access": {"results": [
                {"title": title, "abstract": "Python software testing is mentioned here.",
                 "url": f"https://doaj.org/article/{index}"}
                for index, title in enumerate(titles)
            ]},
        })
        self.assertEqual(report["high_overlap_count"], 0)
        self.assertEqual(report["candidates"][0]["overlap"], "partial")
        self.assertEqual(report["candidates"][0]["matched_title_terms"], ["software"])
        self.assertEqual([item["overlap"] for item in report["candidates"][1:]],
                         ["excerpt_only", "excerpt_only"])

    def test_runtime_records_review_without_model_or_extra_network_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = LearningGoalStore(root)
            goal = store.create("Python software testing", public_research_allowed=True)
            target = {"target_kind": "learning_goal", "learning_goal_id": goal["goal_id"], "topic": goal["topic"]}
            first = lambda *_a, **_k: {"results": [{"title": "Software testing", "url": "https://en.wikipedia.org/a"}], "metrics": {"api_requests": 1}}
            second = lambda *_a, **_k: {"results": [{"title": "Python software testing", "url": "https://doaj.org/a"}], "metrics": {"api_requests": 1}}
            result = run_learning_goal_target(root, target, knowledge_searcher=first, open_access_searcher=second, now_epoch=2_000_000_000)
            persisted = json.loads(Path(result["artifact"]).read_text(encoding="utf-8"))
            self.assertEqual(result["api_requests"], 2)
            self.assertEqual(result["metered_model_requests"], 0)
            self.assertEqual(persisted["source_review"]["high_overlap_count"], 1)
            self.assertFalse(persisted["source_review"]["verified_claims_recorded"])
            self.assertFalse(result["verified_synthesis_performed"])

    def test_local_only_goal_does_not_claim_public_source_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            goal = LearningGoalStore(root).create("Private topic")
            target = {"target_kind": "learning_goal", "learning_goal_id": goal["goal_id"], "topic": goal["topic"]}
            report = run_learning_goal_target(root, target, now_epoch=2_000_000_000)
            self.assertEqual(report["outcome"], "insufficient_local_knowledge")
            self.assertIsNone(report["source_review"])


if __name__ == "__main__":
    unittest.main()
