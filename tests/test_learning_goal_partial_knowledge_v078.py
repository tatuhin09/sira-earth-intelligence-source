"""A related fact must not end an owner's broader learning program."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.knowledge_runtime import resolve_knowledge_query
from sira.learning_goals import LearningGoalStore
from sira.learning_goal_runtime import run_learning_goal_target


class PartialGoalKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        store = KnowledgeConsolidationStore(self.root)
        for source_id, url in (
            ("S1", "https://docs.python.org/3/library/unittest.html"),
            ("S2", "https://docs.pytest.org/en/stable/getting-started.html"),
        ):
            store.record_evidence(
                "claim.python.discovery", "Python unittest and pytest both support test discovery.",
                evidence_id="evidence_" + source_id, source_id=source_id, source_url=url,
                confidence=.85, verifier_kind="verified_quote", evidence_sha256="a" * 64,
                retrieved_at="2026-09-25T00:00:00+00:00", verified=True,
            )
        self.assertEqual(store.consolidate("claim.python.discovery").status, "consolidated")

    def tearDown(self):
        self.temp.cleanup()

    def test_related_claim_does_not_stop_public_goal_research(self):
        goal = LearningGoalStore(self.root).create(
            "Python software testing", public_research_allowed=True
        )
        seen = []

        def search(_root, query, *, max_results):
            seen.append((query, max_results))
            return {"results": [{"title": "Public source", "url": "https://example.org/guide"}],
                    "metrics": {"api_requests": 1}}

        report = run_learning_goal_target(
            self.root, {"target_kind": "learning_goal", "learning_goal_id": goal["goal_id"],
                        "topic": goal["topic"]}, now_epoch=2_000_000_000.0,
            knowledge_searcher=search, open_access_searcher=search,
        )
        self.assertEqual(seen, [(goal["topic"], 3)] * 2)
        self.assertEqual(report["outcome"], "sources_discovered_needs_verification")
        self.assertEqual(report["api_requests"], 2)
        self.assertEqual(report["local_context"]["result_count"], 1)
        self.assertEqual(report["goal_mastery"], "partial")
        self.assertFalse(report["verified_synthesis_performed"])

    def test_related_claim_remains_available_for_private_goal_without_public_search(self):
        goal = LearningGoalStore(self.root).create("Python software testing")
        report = run_learning_goal_target(
            self.root, {"target_kind": "learning_goal", "learning_goal_id": goal["goal_id"],
                        "topic": goal["topic"]}, now_epoch=2_000_000_000.0,
            knowledge_searcher=lambda *_a, **_k: self.fail("No public search allowed"),
            open_access_searcher=lambda *_a, **_k: self.fail("No public search allowed"),
        )
        self.assertEqual(report["outcome"], "partial_local_knowledge_reused")
        self.assertEqual(report["local_context"]["result_count"], 1)
        self.assertEqual(report["api_requests"], 0)

    def test_ordinary_query_keeps_local_first_behavior(self):
        report = resolve_knowledge_query(self.root, "Python software testing")
        self.assertEqual(report["mode"], "local_reuse")
        self.assertEqual(report["api_requests"], 0)


if __name__ == "__main__":
    unittest.main()
