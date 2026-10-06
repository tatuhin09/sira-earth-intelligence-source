"""Common connector words and internal keys are not evidence of relevance."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.knowledge_runtime import knowledge_context_for_query, resolve_knowledge_query
from sira.learning_goals import LearningGoalStore
from sira.learning_goal_runtime import run_learning_goal_target


class KnowledgeContextRelevanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.claim = "Python unittest and pytest both support test discovery."
        self.knowledge = KnowledgeConsolidationStore(self.root)
        for source_id, url in (
            ("S1", "https://docs.python.org/3/library/unittest.html"),
            ("S2", "https://docs.pytest.org/en/stable/getting-started.html"),
        ):
            self.knowledge.record_evidence(
                "claim.python.discovery", self.claim,
                evidence_id="evidence_" + source_id, source_id=source_id,
                source_url=url, confidence=.85, verifier_kind="verified_quote",
                evidence_sha256="a" * 64,
                retrieved_at="2026-09-26T00:00:00+00:00", verified=True,
            )
        self.assertEqual(self.knowledge.consolidate("claim.python.discovery").status,
                         "consolidated")

    def test_unrelated_cybersecurity_query_has_no_local_claim(self):
        topic = "Cybersecurity fundamentals and defense"
        self.assertEqual(knowledge_context_for_query(self.root, topic)["results"], [])
        self.assertEqual(resolve_knowledge_query(self.root, topic)["mode"], "local_miss")

        goal = LearningGoalStore(self.root).create(topic, public_research_allowed=True)
        goal = LearningGoalStore(self.root).set_plan(
            goal["goal_id"], ["Risk assessment and threat modeling", "Incident response"])
        report = run_learning_goal_target(
            self.root, {"target_kind": "learning_goal", "learning_goal_id": goal["goal_id"],
                        "topic": topic}, now_epoch=2_000_000_000,
            knowledge_searcher=lambda *_a, **_k: {"results": [], "metrics": {"api_requests": 0}},
            open_access_searcher=lambda *_a, **_k: {"results": [], "metrics": {"api_requests": 0}},
        )
        self.assertEqual(report["local_context"]["result_count"], 0)
        self.assertEqual(report["goal_mastery"], "unverified")
        self.assertEqual(report["verified_claim_count"], 0)

    def test_real_topic_word_still_finds_partial_verified_claim(self):
        rows = knowledge_context_for_query(self.root, "Python software testing")["results"]
        self.assertEqual([row["claim"] for row in rows], [self.claim])

    def test_internal_key_and_stopword_alone_do_not_match(self):
        self.assertEqual(self.knowledge.search("claim"), [])
        self.assertEqual(self.knowledge.search("and"), [])


if __name__ == "__main__":
    unittest.main()
