"""A blocked DOAJ page may be replaced by independently hosted public text."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goal_general_learning import collect_and_verify_goal_sources
from sira.learning_goals import LearningGoalStore
from sira.learning_goal_runtime import _default_goal_open_access_search


WIKI = "https://en.wikipedia.org/wiki/Software_testing"
DOAJ = "https://doaj.org/article/testing"
ARXIV = "https://arxiv.org/abs/2601.01234"
CLAIM = "Software testing checks whether an application behaves as expected."


class ArxivSourceFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.goal = LearningGoalStore(self.root).create(
            "Software testing", public_research_allowed=True,
        )
        self.research = {
            "knowledge": {"results": [{"title": "Software testing", "url": WIKI}]},
            "open_access": {"results": [
                {"title": "Software testing cases", "url": DOAJ,
                 "provider": "doaj", "abstract": CLAIM},
                {"title": "Software testing methods", "url": ARXIV,
                 "provider": "arxiv", "abstract": CLAIM},
            ]},
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_arxiv_document_replaces_blocked_doaj_page(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if url == DOAJ:
                raise HTTPError(url, 403, "Forbidden", None, None)
            return url, "text/html", ("<html><main><p>" + CLAIM +
                                       "</p></main></html>").encode()

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=fetch,
        )
        self.assertEqual(set(calls), {WIKI, DOAJ, ARXIV})
        self.assertEqual(result["api_requests"], 3)
        self.assertEqual(result["status"], "verified_knowledge_recorded")
        self.assertEqual(result["verified_claim_count"], 1)
        self.assertEqual({x["host"] for x in result["documents"]},
                         {"en.wikipedia.org", "arxiv.org"})
        self.assertEqual(result["source_failures"][0]["code"], "http_403")
        rows = KnowledgeConsolidationStore(self.root).search("Software testing", limit=5)
        self.assertEqual(rows[0]["host_count"], 2)

    def test_arxiv_redirect_or_wrong_content_stays_unverified(self):
        def fetch(url):
            if url == DOAJ:
                raise HTTPError(url, 403, "Forbidden", None, None)
            if url == ARXIV:
                return "https://elsewhere.example/", "text/html", b"<p>bad</p>"
            return url, "text/html", ("<html><main><p>" + CLAIM +
                                       "</p></main></html>").encode()

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=fetch,
        )
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(len(result["source_failures"]), 2)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_more_than_three_search_results_are_considered(self):
        self.research["open_access"]["results"] = [
            {"title": "Unrelated research", "url": f"https://doaj.org/article/{i}",
             "provider": "doaj", "abstract": "unrelated"} for i in range(3)
        ] + [{"title": "Software testing cases", "url": ARXIV, "provider": "arxiv"}]
        calls = []

        def fetch(url):
            calls.append(url)
            return url, "text/html", ("<html><main><p>" + CLAIM +
                                       "</p></main></html>").encode()

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=fetch,
        )
        self.assertEqual(set(calls), {WIKI, ARXIV})
        self.assertEqual(result["verified_claim_count"], 1)

    def test_default_goal_search_uses_two_free_providers(self):
        with patch("sira.learning_goal_runtime.search_open_access_free",
                   return_value={"metrics": {"api_requests": 0}}) as search:
            _default_goal_open_access_search(self.root, "Software testing", max_results=3)
        providers = search.call_args.kwargs["providers"]
        self.assertEqual([p.name for p in providers], ["doaj", "arxiv"])
        self.assertEqual(search.call_args.kwargs["max_results"], 3)

    def test_revocation_before_fallback_skips_arxiv_and_knowledge_write(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if url == DOAJ:
                LearningGoalStore(self.root).set_policy(
                    self.goal["goal_id"], public_research_allowed=False,
                )
                raise HTTPError(url, 403, "Forbidden", None, None)
            return url, "text/html", ("<html><main><p>" + CLAIM +
                                       "</p></main></html>").encode()

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=fetch,
        )
        self.assertNotIn(ARXIV, calls)
        self.assertEqual(result["status"], "research_permission_revoked")
        self.assertEqual(result["verified_claim_count"], 0)


if __name__ == "__main__":
    unittest.main()
