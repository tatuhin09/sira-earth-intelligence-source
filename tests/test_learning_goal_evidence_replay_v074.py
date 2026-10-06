"""Saved learning-goal research can produce quote evidence without new requests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_goal_evidence_replay import replay_learning_goal_evidence
from sira.learning_goals import LearningGoalStore


class LearningGoalEvidenceReplayV074Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.goal = LearningGoalStore(self.root).create("Python software testing", public_research_allowed=True)
        self.filename = self.goal["goal_id"] + "_" + "a" * 32 + ".json"
        self.path = self.root / "memory/learning_goal_reports" / self.filename
        self.path.parent.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, research):
        data = {"schema": "sira.learning_goal_research.v1", "status": "completed",
                "learning_goal_id": self.goal["goal_id"], "topic": self.goal["topic"],
                "research": research, "verified_synthesis_performed": False}
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_existing_broad_results_do_not_create_fact_memory_or_quote_evidence(self):
        self.save({"knowledge": {"results": [
            {"title": "Software testing", "url": "https://en.wikipedia.org/wiki/Software_testing", "description": "Testing software"}
        ]}, "open_access": {"results": [
            {"title": "Underwater vehicle recovery", "url": "https://doaj.org/article/1",
             "abstract": "Python software testing mentioned incidentally."}
        ]}})
        result = replay_learning_goal_evidence(self.root, self.filename)
        self.assertEqual(result["status"], "insufficient_relevant_sources")
        self.assertEqual(result["quotes"], [])
        self.assertEqual(result["api_requests"], 0)
        self.assertEqual(result["metered_model_requests"], 0)
        self.assertFalse(result["verified_claims_recorded"])
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())
        self.assertEqual(result["source_report_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertTrue(Path(result["artifact"]).is_file())

    def test_exact_abstract_quotes_and_two_hosts_are_still_not_verified_facts(self):
        abstract_a = "Unit tests isolate Python functions.\nSoftware testing checks behavior."
        abstract_b = "Python software testing can use small repeatable cases."
        self.save({"open_access": {"results": [
            {"title": "Python software testing methods", "url": "https://papers.example.org/a",
             "abstract": abstract_a, "retrieved_at": "2026-09-25T00:00:00+00:00"},
            {"title": "Python software testing practice", "url": "https://journal.example.com/b",
             "abstract": abstract_b, "retrieved_at": "2026-09-25T00:00:00+00:00"},
        ]}})
        result = replay_learning_goal_evidence(self.root, self.filename)
        self.assertEqual(result["status"], "quotes_need_claim_verification")
        self.assertEqual(result["quote_source_count"], 2)
        self.assertEqual(result["distinct_quote_host_count"], 2)
        for quote in result["quotes"]:
            document = abstract_a if quote["source_index"] == 0 else abstract_b
            self.assertEqual(document[quote["start"]:quote["end"]], quote["quote"])
            self.assertEqual(quote["content_sha256"], hashlib.sha256(document.encode()).hexdigest())
        self.assertFalse(result["verified_claims_recorded"])
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())
        self.assertEqual(result, replay_learning_goal_evidence(self.root, self.filename))

    def test_rejects_mismatched_goal_and_symlinked_report(self):
        self.save({})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["learning_goal_id"] = "lg_" + "b" * 32
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            replay_learning_goal_evidence(self.root, self.filename)
        self.path.unlink()
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(data), encoding="utf-8")
        self.path.symlink_to(outside)
        with self.assertRaises(ValueError):
            replay_learning_goal_evidence(self.root, self.filename)
        self.assertFalse((self.root / "memory/learning_goal_evidence").exists())


if __name__ == "__main__":
    unittest.main()
