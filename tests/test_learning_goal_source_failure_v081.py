"""A blocked DOAJ article page must not turn metadata into a verified claim."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goal_general_learning import collect_and_verify_goal_sources
from sira.learning_goal_runtime import run_learning_goal_target
from sira.learning_goals import LearningGoalStore

WIKI = "https://en.wikipedia.org/wiki/Software_testing"
DOAJ = "https://doaj.org/article/0253d579f7404b5da167539de1c3690b"


class LearningGoalSourceFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.goal = LearningGoalStore(self.root).create(
            "Software testing", public_research_allowed=True,
        )
        self.research = {
            "knowledge": {"results": [{"title": "Software testing", "url": WIKI}],
                          "metrics": {"api_requests": 1}},
            "open_access": {"results": [{
                "title": "Software testing research", "url": DOAJ,
                "abstract": "Software testing checks whether an application behaves as expected.",
                "provider": "doaj", "retrieved_at": "2026-09-26T00:00:00+00:00",
            }], "metrics": {"api_requests": 1}},
        }

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def fetch(url):
        if url == DOAJ:
            raise HTTPError(url, 403, "Forbidden", None, None)
        return url, "text/html", (
            "<html><main><p>Software testing checks whether an application "
            "behaves as expected.</p></main></html>"
        ).encode()

    def test_blocked_article_retains_unverified_evidence_and_safe_failure_code(self):
        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=self.fetch,
        )
        self.assertEqual(result["status"], "independent_source_unavailable")
        self.assertEqual(result["api_requests"], 2)
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(result["source_failures"], [
            {"host": "doaj.org", "url": DOAJ, "code": "http_403"},
        ])
        saved = json.loads(Path(result["artifact"]).read_text(encoding="utf-8"))
        self.assertEqual(len(saved["documents"]), 1)
        self.assertEqual(saved["documents"][0]["host"], "en.wikipedia.org")
        self.assertEqual(saved["metadata_evidence"][0]["abstract"],
                         self.research["open_access"]["results"][0]["abstract"])
        self.assertIs(saved["metadata_evidence"][0]["verified"], False)
        self.assertIs(saved["verified_claims_recorded"], False)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_runtime_records_failure_without_claiming_learning(self):
        result = run_learning_goal_target(
            self.root,
            {"target_kind": "learning_goal", "learning_goal_id": self.goal["goal_id"],
             "topic": self.goal["topic"]},
            knowledge_searcher=lambda *_a, **_k: self.research["knowledge"],
            open_access_searcher=lambda *_a, **_k: self.research["open_access"],
            document_fetcher=self.fetch,
            now_epoch=2_000_000_000.0,
        )
        self.assertEqual(result["outcome"], "independent_source_unavailable")
        self.assertEqual(result["api_requests"], 4)
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(result["metered_model_requests"], 0)
        self.assertEqual(result["document_review"]["source_failures"][0]["code"], "http_403")

    def test_invalid_abstract_does_not_break_partial_source_record(self):
        self.research["open_access"]["results"][0]["abstract"] = "\ud800" * 50
        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=self.fetch,
        )
        self.assertEqual(result["status"], "independent_source_unavailable")
        self.assertEqual(result["metadata_evidence"], [])
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual(result["verified_claim_count"], 0)


if __name__ == "__main__":
    unittest.main()
