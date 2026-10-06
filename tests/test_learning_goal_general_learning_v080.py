"""A public owner goal can retain only corroborated, exact source statements."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goals import LearningGoalStore
from sira.learning_goal_runtime import run_learning_goal_target
from sira.learning_goal_general_learning import (
    collect_and_verify_goal_sources, verify_shared_statements,
)


WIKI = "https://en.wikipedia.org/wiki/Software_testing"
DOAJ = "https://doaj.org/article/testing"
CLAIM = "Software testing checks whether an application behaves as expected."


def html(claim: str) -> bytes:
    return ("<html><main><p>Software testing helps teams find defects.</p>"
            f"<p>{claim}</p></main></html>").encode()


def pdf(claim: str | None) -> bytes:
    content = (f"BT /F1 12 Tf 72 720 Td ({claim}) Tj ET".encode("ascii")
               if claim is not None else b"q Q")
    return (b"%PDF-1.4\n1 0 obj<< /Length " + str(len(content)).encode("ascii")
            + b" >>stream\n" + content + b"\nendstream\nendobj\n%%EOF")


class GeneralGoalLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.goal = LearningGoalStore(self.root).create(
            "Software testing", public_research_allowed=True,
        )
        self.results = {
            "knowledge": {"results": [{"title": "Software testing", "url": WIKI}],
                          "metrics": {"api_requests": 1}},
            "open_access": {"results": [{"title": "Software testing methods", "url": DOAJ}],
                            "metrics": {"api_requests": 1}},
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_runtime_reads_two_public_hosts_and_reuses_verified_claim(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return url, "text/html", html(CLAIM)

        target = {"target_kind": "learning_goal", "learning_goal_id": self.goal["goal_id"],
                  "topic": self.goal["topic"]}
        result = run_learning_goal_target(
            self.root, target, now_epoch=2_000_000_000.0,
            knowledge_searcher=lambda *_a, **_k: self.results["knowledge"],
            open_access_searcher=lambda *_a, **_k: self.results["open_access"],
            document_fetcher=fetch,
        )
        self.assertEqual(calls, [WIKI, DOAJ])
        self.assertEqual(result["outcome"], "verified_knowledge_recorded")
        self.assertEqual(result["api_requests"], 4)
        self.assertEqual(result["metered_model_requests"], 0)
        self.assertEqual(result["verified_claim_count"], 1)
        self.assertEqual(result["goal_mastery"], "partial")
        self.assertFalse(result["verified_synthesis_performed"])
        self.assertFalse(result["promotion_performed"])
        rows = KnowledgeConsolidationStore(self.root).search("Software testing", limit=5)
        self.assertEqual([row["claim_text"] for row in rows], [CLAIM])
        self.assertEqual(rows[0]["host_count"], 2)

    def test_general_learning_worker_can_read_pdf_sources(self):
        raw_pdf = pdf(CLAIM)
        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.results,
            fetcher=lambda url: (url, "application/pdf", raw_pdf),
        )
        self.assertEqual(result["verified_claim_count"], 1)
        self.assertTrue(all("[Page 1]" in row["text"] for row in result["documents"]))

    def test_scanned_pdf_failure_explains_that_ocr_is_needed(self):
        raw_pdf = pdf(None)
        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.results,
            fetcher=lambda url: (url, "application/pdf", raw_pdf),
        )
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual({row["code"] for row in result["source_failures"]}, {"pdf_no_text"})

    def test_different_statements_remain_unverified(self):
        def fetch(url):
            other = "Software testing is a process used to evaluate computer programs."
            return url, "text/html", html(CLAIM if url == WIKI else other)

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.results,
            fetcher=fetch,
        )
        self.assertEqual(result["status"], "unverified_source_statements")
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_untrusted_host_and_redirect_are_never_accepted(self):
        self.results["open_access"]["results"][0]["url"] = "https://localhost/private"
        calls = []
        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.results,
            fetcher=lambda url: (calls.append(url) or (url, "text/html", html(CLAIM))),
        )
        self.assertEqual(calls, [WIKI])
        self.assertEqual(result["api_requests"], 1)
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(result["status"], "insufficient_independent_sources")

        self.results["open_access"]["results"][0]["url"] = DOAJ
        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.results,
            fetcher=lambda url: ("https://elsewhere.example/redirect", "text/html", html(CLAIM)),
        )
        self.assertEqual(result["verified_claim_count"], 0)

    def test_permission_revocation_before_second_request_blocks_knowledge_write(self):
        calls = []

        def fetch(url):
            calls.append(url)
            LearningGoalStore(self.root).set_policy(
                self.goal["goal_id"], public_research_allowed=False,
            )
            return url, "text/html", html(CLAIM)

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"], self.results,
            fetcher=fetch,
        )
        self.assertEqual(calls, [WIKI])
        self.assertEqual(result["status"], "research_permission_revoked")
        self.assertEqual(result["verified_claim_count"], 0)

    def test_mutated_document_digest_cannot_be_verified(self):
        docs = [
            {"url": url, "title": "Software testing", "host": host,
             "text": CLAIM, "content_sha256": hashlib.sha256(CLAIM.encode()).hexdigest(),
             "retrieved_at": "2026-09-25T00:00:00+00:00"}
            for url, host in ((WIKI, "en.wikipedia.org"), (DOAJ, "doaj.org"))
        ]
        docs[1]["text"] += " changed"
        with self.assertRaises(ValueError):
            verify_shared_statements(self.root, self.goal["goal_id"],
                                     self.goal["topic"], docs)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_non_english_topic_and_sentence_are_not_excluded(self):
        topic = "বাংলা ভাষার পরীক্ষা"
        claim = "বাংলা ভাষার পরীক্ষা শিক্ষার্থীদের ভাষার দক্ষতা বোঝার জন্য বিভিন্ন প্রশ্ন ব্যবহার করে।"
        goal = LearningGoalStore(self.root).create(topic, public_research_allowed=True)
        docs = [
            {"url": url, "title": topic, "host": host, "text": claim,
             "content_sha256": hashlib.sha256(claim.encode()).hexdigest(),
             "retrieved_at": "2026-09-25T00:00:00+00:00"}
            for url, host in ((WIKI, "en.wikipedia.org"), (DOAJ, "doaj.org"))
        ]
        result = verify_shared_statements(self.root, goal["goal_id"], topic, docs)
        self.assertEqual(result["verified_claim_count"], 1)


if __name__ == "__main__":
    unittest.main()
