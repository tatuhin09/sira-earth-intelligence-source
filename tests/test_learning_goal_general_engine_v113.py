"""A77 general, bounded learning from exact readable multi-source evidence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goal_evidence_progress import verified_study_step_evidence
from sira.learning_goal_general_engine import (
    assess_general_documents, has_general_source_gap, run_general_learning,
)
from sira.learning_goal_runtime import run_learning_goal_target
from sira.learning_goal_skill_candidates import advisory_skill_candidates
from sira.learning_goals import LearningGoalStore
from sira.learning_consolidation import LearningConsolidationStore


QUESTION = "Urban tree canopy and daytime heat"
CLAIM = "Urban tree canopy can reduce daytime heat in neighborhoods by providing shade."
CONTRA = "Urban tree canopy can not reduce daytime heat in neighborhoods by providing shade."
URLS = (
    "https://www.nasa.gov/earth/urban-tree-canopy",
    "https://www.noaa.gov/climate/urban-tree-canopy",
)


def html(sentence: str) -> bytes:
    return ("<html><main><p>" + sentence + "</p></main></html>").encode()


def pdf(text: str) -> bytes:
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content + b"\nendstream",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(payload)


class GeneralLearningEngineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create("Urban climate planning", public_research_allowed=True)
        self.store.set_plan(self.goal["goal_id"], [QUESTION, "Stormwater and drainage"])
        self.research = {
            "knowledge": {"results": [{"title": QUESTION, "url": URLS[0]}]},
            "open_access": {"results": [{"title": QUESTION, "url": URLS[1]}]},
        }

    def run_engine(self, fetcher=None):
        return run_general_learning(
            self.root, self.goal["goal_id"], self.research, question=QUESTION,
            fetcher=fetcher or (lambda url: (url, "text/html", html(CLAIM))),
        )

    def doc(self, url: str, sentence: str, *, age_days=0):
        text = sentence
        return {"url": url, "host": url.split("/")[2], "title": QUESTION,
                "text": text, "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "retrieved_at": (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat(),
                "verified": False}

    def test_arbitrary_topic_general_path_records_one_bounded_claim(self):
        result = self.run_engine()
        self.assertEqual(result["status"], "verified_knowledge_recorded")
        self.assertEqual(result["verified_claim_count"], 1)
        self.assertEqual(result["question"], QUESTION)
        self.assertEqual(result["source_classes"], ["official_primary", "official_primary"])
        self.assertEqual(result["resource_usage"]["paid_requests"], 0)
        self.assertFalse(result["skill_activated"])
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])

    def test_general_research_worker_reads_pdf_sources_before_verification(self):
        raw_pdf = pdf(CLAIM)
        result = self.run_engine(lambda url: (url, "application/pdf", raw_pdf))
        self.assertEqual(result["status"], "verified_knowledge_recorded")
        self.assertEqual(result["verified_claim_count"], 1)
        self.assertTrue(all("[Page 1]" in row["text"] for row in result["documents"]))

    def test_discovery_only_and_unreadable_sources_do_not_verify(self):
        result = self.run_engine(lambda url: (url, "text/html", b"<html><main></main></html>"))
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(result["status"], "insufficient_readable_sources")
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_one_weak_source_cannot_satisfy_two_host_policy(self):
        result = self.run_engine(lambda url: (url, "text/html", html(CLAIM))
                                 if url == URLS[0] else (url, "text/plain", b"metadata"))
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(result["status"], "insufficient_independent_sources")

    def test_contradiction_is_preserved_and_blocks_consensus(self):
        result = self.run_engine(lambda url: (url, "text/html",
                                                html(CLAIM if url == URLS[0] else CONTRA)))
        self.assertEqual(result["status"], "contradictory_evidence")
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(len(result["contradictions"]), 1)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_contradiction_blocks_an_otherwise_shared_sentence(self):
        shared = "Urban tree canopy and daytime heat are studied in city neighborhoods. "
        result = self.run_engine(lambda url: (url, "text/html",
            html(shared + (CLAIM if url == URLS[0] else CONTRA))))
        self.assertEqual(result["status"], "contradictory_evidence")
        self.assertEqual(result["verified_claim_count"], 0)

    def test_unmatched_relevant_statements_make_shared_text_ambiguous(self):
        shared = "Urban tree canopy and daytime heat are studied in city neighborhoods. "
        alternative = ("Urban tree canopy has uncertain effects on daytime heat "
                       "in several neighborhoods.")
        result = self.run_engine(lambda url: (url, "text/html",
            html(shared + (CLAIM if url == URLS[0] else alternative))))
        self.assertEqual(result["status"], "ambiguous_evidence")
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertTrue(result["unmatched_statements"])

    def test_stale_evidence_is_explicitly_insufficient(self):
        decision = assess_general_documents(QUESTION,
            [self.doc(URLS[0], CLAIM, age_days=400),
             self.doc(URLS[1], CLAIM, age_days=400)])
        self.assertEqual(decision["status"], "stale_evidence")
        self.assertEqual(decision["claim"], None)

    def test_modified_source_text_cannot_be_promoted(self):
        docs = [self.doc(URLS[0], CLAIM), self.doc(URLS[1], CLAIM)]
        docs[1]["text"] += " Altered after the source digest was recorded."
        with self.assertRaises(ValueError):
            assess_general_documents(QUESTION, docs)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_exact_saved_provenance_feeds_goal_progress_and_candidate(self):
        result = self.run_engine()
        artifact = Path(result["artifact"])
        saved = json.loads(artifact.read_text())
        self.assertEqual(saved["schema"], "sira.learning_goal_general_documents.v1")
        self.assertEqual(saved["research_query"], QUESTION)
        self.assertEqual(saved["status"], "quotes_need_claim_verification")
        self.assertEqual({d["host"] for d in saved["documents"]},
                         {"www.nasa.gov", "www.noaa.gov"})
        self.assertEqual({d["discovery_provider"] for d in saved["documents"]},
                         {"knowledge", "open_access"})
        for row in saved["documents"]:
            self.assertEqual(hashlib.sha256(row["text"].encode()).hexdigest(),
                             row["content_sha256"])
        evidence = verified_study_step_evidence(self.root, self.store.get(self.goal["goal_id"]))
        self.assertIn(CLAIM, evidence[QUESTION])
        candidates = advisory_skill_candidates(self.root, self.store.get(self.goal["goal_id"]))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["claim"], CLAIM)
        self.assertFalse(candidates[0]["skill_activated"])
        self.assertFalse((self.root / "memory/learning_skill_lifecycle").exists())

    def test_irrelevant_metadata_or_source_text_is_rejected(self):
        self.research["open_access"]["results"][0]["title"] = "Unrelated marine geology"
        result = self.run_engine()
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(result["status"], "insufficient_independent_sources")
        self.research["open_access"]["results"][0]["title"] = QUESTION
        irrelevant = "Ocean salinity influences marine ecosystems across different latitudes."
        result = self.run_engine(lambda url: (url, "text/html",
                                               html(CLAIM if url == URLS[0] else irrelevant)))
        self.assertEqual(result["verified_claim_count"], 0)

    def test_no_specialized_assessor_required(self):
        with patch("sira.learning_goal_curated_runtime.verify_curated_saved_claim",
                   side_effect=AssertionError("No topic adapter must run")):
            result = self.run_engine()
        self.assertEqual(result["verified_claim_count"], 1)

    def test_provider_failure_bounded_and_fail_closed(self):
        calls = []
        def failed(url):
            calls.append(url)
            raise URLError("offline")
        result = self.run_engine(failed)
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(result["api_requests"], 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["resource_usage"]["paid_requests"], 0)

    def test_owner_revocation_stops_reading_and_cannot_verify(self):
        calls = []
        def revoke(url):
            calls.append(url)
            self.store.set_policy(self.goal["goal_id"], public_research_allowed=False)
            return url, "text/html", html(CLAIM)
        result = self.run_engine(revoke)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "research_permission_revoked")
        self.assertEqual(result["verified_claim_count"], 0)

    def test_runtime_fallback_handles_new_hosts_without_changing_known_adapter(self):
        self.assertTrue(has_general_source_gap(QUESTION, self.research))
        report = run_learning_goal_target(
            self.root,
            {"target_kind": "learning_goal", "learning_goal_id": self.goal["goal_id"],
             "topic": self.goal["topic"]},
            knowledge_searcher=lambda *_a, **_k: self.research["knowledge"],
            open_access_searcher=lambda *_a, **_k: self.research["open_access"],
            document_fetcher=lambda url: (url, "text/html", html(CLAIM)),
        )
        self.assertEqual(report["outcome"], "verified_knowledge_recorded")
        self.assertEqual(report["verified_claim_count"], 1)
        self.assertEqual(report["document_review"]["engine"], "general_verified_learning_v1")


if __name__ == "__main__":
    unittest.main()
