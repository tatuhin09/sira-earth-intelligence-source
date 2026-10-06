"""A prior partial source can guide a fresh, independent document read."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goal_general_learning import collect_and_verify_goal_sources
from sira.learning_goals import LearningGoalStore


WIKI = "https://en.wikipedia.org/wiki/Network_security"
ARXIV = "https://arxiv.org/abs/2401.01234"
SENTENCE = ("Network security controls help protect computer networks "
            "from unauthorized access and support defensive monitoring.")
HTML = f"<html><main><p>{SENTENCE}</p></main></html>".encode()


class CachedLearningSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create("Network security controls", public_research_allowed=True)
        self.first = {"knowledge": {"results": [
            {"title": "Network security controls", "url": WIKI}
        ]}, "open_access": {"results": []}}
        self.second = {"knowledge": {"results": []}, "open_access": {"results": [
            {"title": "Network security controls", "url": ARXIV, "provider": "arxiv"}
        ]}}

    def _first_cycle(self):
        return collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.first,
            fetcher=lambda url: (url, "text/html", HTML),
        )

    def test_previous_url_is_refetched_when_a_new_independent_host_appears(self):
        first = self._first_cycle()
        self.assertEqual(first["verified_claim_count"], 0)
        calls = []

        def fetch(url):
            calls.append(url)
            return url, "text/html", HTML

        second = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.second,
            fetcher=fetch,
        )
        self.assertEqual(second["status"], "verified_knowledge_recorded")
        self.assertEqual(second["verified_claim_count"], 1)
        self.assertEqual(second["claims"][0]["claim"], SENTENCE)
        self.assertEqual(set(second["claims"][0]["source_urls"]), {WIKI, ARXIV})
        self.assertEqual(set(calls), {WIKI, ARXIV})
        self.assertEqual(second["api_requests"], 2)
        self.assertEqual(second["metered_model_requests"], 0)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 1)

    def test_mutated_partial_artifact_is_not_used_for_a_claim(self):
        first = self._first_cycle()
        path = Path(first["artifact"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["documents"][0]["text"] = "A changed source that does not match the original digest."
        path.write_text(json.dumps(payload), encoding="utf-8")
        calls = []

        def fetch(url):
            calls.append(url)
            return url, "text/html", HTML

        second = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.second,
            fetcher=fetch,
        )
        self.assertEqual(calls, [ARXIV])
        self.assertEqual(second["verified_claim_count"], 0)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_changed_page_is_refetched_and_old_text_cannot_verify(self):
        self._first_cycle()
        calls = []

        def fetch(url):
            calls.append(url)
            body = (HTML if url == ARXIV else
                    b"<main><p>Public information changed since the first visit.</p></main>")
            return url, "text/html", body

        second = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.second,
            fetcher=fetch,
        )
        self.assertEqual(set(calls), {WIKI, ARXIV})
        self.assertEqual(second["verified_claim_count"], 0)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_previous_query_does_not_cross_into_another_study_step(self):
        self.store.set_plan(self.goal["goal_id"], [
            "Network security controls", "Identity and access management",
        ])
        self._first_cycle()
        research = {"knowledge": {"results": []}, "open_access": {"results": [
            {"title": "Identity and access management", "url": ARXIV, "provider": "arxiv"}
        ]}}
        calls = []

        def fetch(url):
            calls.append(url)
            return url, "text/html", HTML

        second = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], research,
            focus="Identity and access management", fetcher=fetch,
        )
        self.assertEqual(calls, [ARXIV])
        self.assertEqual(second["verified_claim_count"], 0)

    def test_oversized_url_in_partial_artifact_is_not_requested(self):
        first = self._first_cycle()
        path = Path(first["artifact"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["documents"][0]["url"] = WIKI + "?q=" + "x" * 3000
        path.write_text(json.dumps(payload), encoding="utf-8")
        calls = []

        def fetch(url):
            calls.append(url)
            return url, "text/html", HTML

        second = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.second,
            fetcher=fetch,
        )
        self.assertEqual(calls, [ARXIV])
        self.assertEqual(second["verified_claim_count"], 0)

    def test_revocation_after_current_source_prevents_cached_fetch_and_write(self):
        self._first_cycle()
        calls = []

        def fetch(url):
            calls.append(url)
            self.store.set_policy(self.goal["goal_id"], public_research_allowed=False)
            return url, "text/html", HTML

        second = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.second,
            fetcher=fetch,
        )
        self.assertEqual(second["status"], "research_permission_revoked")
        self.assertEqual(calls, [ARXIV])
        self.assertEqual(second["verified_claim_count"], 0)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)


if __name__ == "__main__":
    unittest.main()
