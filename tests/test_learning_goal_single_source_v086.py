"""A single relevant document remains auditable without becoming knowledge."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goal_general_learning import collect_and_verify_goal_sources
from sira.learning_goal_runtime import run_learning_goal_target
from sira.learning_goals import LearningGoalStore


WIKI = "https://en.wikipedia.org/wiki/Network_security"
TEXT = ("<html><main><p>Network security controls help protect computer networks "
        "from unauthorized access and support defensive monitoring.</p></main></html>").encode()


class SingleLearningSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create("Network security controls", public_research_allowed=True)
        self.research = {"knowledge": {"results": [
            {"title": "Network security controls", "url": WIKI}
        ]}, "open_access": {"results": []}}

    def test_one_host_is_read_and_saved_as_unverified_evidence(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return url, "text/html", TEXT

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.research,
            fetcher=fetch,
        )
        self.assertEqual(calls, [WIKI])
        self.assertEqual(result["status"], "insufficient_independent_sources")
        self.assertEqual(result["api_requests"], 1)
        self.assertEqual(result["verified_claim_count"], 0)
        saved = json.loads(Path(result["artifact"]).read_text(encoding="utf-8"))
        self.assertEqual(len(saved["documents"]), 1)
        self.assertEqual(saved["documents"][0]["url"], WIKI)
        self.assertIs(saved["documents"][0]["verified"], False)
        self.assertIs(saved["verified_claims_recorded"], False)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_revoked_permission_disallows_saving_even_one_document(self):
        def fetch(url):
            self.store.set_policy(self.goal["goal_id"], public_research_allowed=False)
            return url, "text/html", TEXT

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.research,
            fetcher=fetch,
        )
        self.assertEqual(result["status"], "research_permission_revoked")
        self.assertEqual(result["api_requests"], 1)
        self.assertNotIn("artifact", result)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_no_relevant_public_candidate_avoids_document_request(self):
        self.research["knowledge"]["results"] = []
        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.research,
            fetcher=lambda _: self.fail("No source available to read"),
        )
        self.assertEqual(result["api_requests"], 0)
        self.assertNotIn("artifact", result)

    def test_planned_runtime_keeps_partial_source_and_does_not_claim_mastery(self):
        self.store.set_plan(self.goal["goal_id"], [
            "Network security controls", "Identity and access management",
        ])
        target = {"target_kind": "learning_goal", "learning_goal_id": self.goal["goal_id"],
                  "topic": self.goal["topic"]}
        result = run_learning_goal_target(
            self.root, target, now_epoch=2_000_000_000.0,
            knowledge_searcher=lambda *_args, **_kwargs: self.research["knowledge"],
            open_access_searcher=lambda *_args, **_kwargs: self.research["open_access"],
            document_fetcher=lambda url: (url, "text/html", TEXT),
        )
        self.assertEqual(result["document_review"]["status"], "insufficient_independent_sources")
        self.assertEqual(result["api_requests"], 1)
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(result["goal_mastery"], "unverified")
        self.assertEqual(result["metered_model_requests"], 0)
        self.assertEqual(self.store.get(self.goal["goal_id"])["study_history"][0]["verified_claim_count"], 0)
        self.assertTrue(Path(result["document_review"]["artifact"]).is_file())


if __name__ == "__main__":
    unittest.main()
