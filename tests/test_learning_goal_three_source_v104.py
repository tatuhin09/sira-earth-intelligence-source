"""A third independent public source can corroborate a generic study claim."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goal_evidence_progress import verified_study_step_evidence
from sira.learning_goal_general_learning import collect_and_verify_goal_sources
from sira.learning_goals import LearningGoalStore


WIKI = "https://en.wikipedia.org/wiki/Software_testing"
DOAJ = "https://doaj.org/article/test-methods"
ARXIV = "https://arxiv.org/abs/2601.01234"
SHARED = "Software testing checks whether an application behaves as expected."
OTHER = "Software testing is a process used to evaluate computer programs."
THIRD = "Software testing can include functional and nonfunctional checks."


class ThirdIndependentSourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.goals = LearningGoalStore(self.root)
        goal = self.goals.create("Software testing", public_research_allowed=True)
        self.goal_id = goal["goal_id"]
        self.goals.set_plan(self.goal_id, ["Software testing", "Fixtures and mocks"])
        self.research = {
            "knowledge": {"results": [{"title": "Software testing A overview", "url": WIKI}]},
            "open_access": {"results": [
                {"title": "Software testing B methods", "url": DOAJ},
                {"title": "Software testing C research", "url": ARXIV},
            ]},
        }

    def collect(self, statements):
        calls = []

        def fetch(url):
            calls.append(url)
            return url, "text/html", ("<html><main><p>" + statements[url] +
                                       "</p></main></html>").encode("utf-8")

        result = collect_and_verify_goal_sources(
            self.root, self.goal_id, "Software testing", self.research,
            fetcher=fetch, focus="Software testing",
        )
        return result, calls

    def test_third_host_can_corroborate_first_and_progress_stays_attributed(self):
        result, calls = self.collect({WIKI: SHARED, DOAJ: OTHER, ARXIV: SHARED})
        self.assertEqual(calls, [WIKI, DOAJ, ARXIV])
        self.assertEqual(result["api_requests"], 3)
        self.assertEqual(result["status"], "verified_knowledge_recorded")
        self.assertEqual(result["verified_claim_count"], 1)
        self.assertEqual(result["claims"][0]["source_urls"], [WIKI, ARXIV])
        self.assertEqual(KnowledgeConsolidationStore(self.root).search("Software testing")[0]["host_count"], 2)
        self.assertEqual(verified_study_step_evidence(
            self.root, self.goals.get(self.goal_id)), {"Software testing": [SHARED]})

    def test_no_third_request_if_first_two_agree(self):
        result, calls = self.collect({WIKI: SHARED, DOAJ: SHARED, ARXIV: THIRD})
        self.assertEqual(calls, [WIKI, DOAJ])
        self.assertEqual(result["verified_claim_count"], 1)

    def test_three_unrelated_documents_remain_unverified(self):
        result, calls = self.collect({WIKI: SHARED, DOAJ: OTHER, ARXIV: THIRD})
        self.assertEqual(calls, [WIKI, DOAJ, ARXIV])
        self.assertEqual(result["status"], "unverified_source_statements")
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_failed_third_source_remains_visible_with_two_unmatched_documents(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if url == ARXIV:
                raise HTTPError(url, 429, "rate limited", {}, None)
            text = SHARED if url == WIKI else OTHER
            return url, "text/html", ("<html><main><p>" + text +
                                       "</p></main></html>").encode("utf-8")

        result = collect_and_verify_goal_sources(
            self.root, self.goal_id, "Software testing", self.research,
            fetcher=fetch, focus="Software testing",
        )
        self.assertEqual(calls, [WIKI, DOAJ, ARXIV])
        self.assertEqual(result["status"], "independent_source_unavailable")
        self.assertEqual(result["source_failures"], [{
            "host": "arxiv.org", "url": ARXIV, "code": "http_429",
        }])
        self.assertEqual(result["verified_claim_count"], 0)


if __name__ == "__main__":
    unittest.main()
