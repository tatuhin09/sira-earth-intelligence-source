"""Only two independent official sources support the narrow testing-scope claim."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.knowledge_runtime import knowledge_context_for_query
from sira.learning_goal_scope_claim import (
    CLAIM, assess_testing_scope_claim, record_testing_scope_claim,
)
from sira.learning_goals import LearningGoalStore


class TestingScopeClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.goals = LearningGoalStore(self.root)
        self.goal_id = self.goals.create(
            "Ethical hacking and authorized penetration testing",
            public_research_allowed=True,
        )["goal_id"]
        self.focus = "Penetration testing scope and authorization"
        self.goals.set_plan(self.goal_id, [self.focus, "Vulnerability validation"])
        self.path = (self.root / "memory" / "learning_goal_documents" /
                     f"{self.goal_id}_{'a' * 32}.json")
        self.pages = [
            {
                "url": "https://cheatsheetseries.owasp.org/cheatsheets/Vulnerability_Disclosure_Cheat_Sheet.html",
                "host": "cheatsheetseries.owasp.org", "title": "Vulnerability Disclosure Cheat Sheet",
                "text": ("If you are carrying out testing under a bug bounty or similar program, "
                         "the organization may have established safe harbor policies, that allow "
                         "you to legally carry out testing, as long as you stay within the scope "
                         "and rules of their program . Make sure that you read the scope carefully."),
            },
            {
                "url": ("https://www.cisa.gov/news-events/news/"
                        "cisa-issues-final-vulnerability-disclosure-policy-directive-federal-agencies"),
                "host": "www.cisa.gov", "title": "CISA Vulnerability Disclosure Policy Directive",
                "text": ("Vulnerability disclosure policies make it easier for the public to know "
                         "where to send a report, what types of testing are authorized for which "
                         "systems, and what communication to expect. They support risk management."),
            },
        ]
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        documents = [
            {**row, "content_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
             "retrieved_at": "2026-09-27T10:00:00+00:00", "verified": False}
            for row in self.pages
        ]
        self.path.write_text(json.dumps({
            "schema": "sira.learning_goal_general_documents.v1",
            "learning_goal_id": self.goal_id,
            "topic": "Ethical hacking and authorized penetration testing",
            "research_query": self.focus, "status": "quotes_need_claim_verification",
            "documents": documents, "metadata_evidence": [], "source_failures": [],
            "verified_claims_recorded": False,
        }), encoding="utf-8")

    def test_read_only_assessment_checks_two_complementary_components(self):
        result = assess_testing_scope_claim(self.root, self.goal_id, self.path)
        self.assertEqual(result["status"], "joint_source_supported")
        self.assertEqual(result["claim"], CLAIM)
        self.assertEqual({s["host"] for s in result["support"]},
                         {"www.cisa.gov", "cheatsheetseries.owasp.org"})
        self.assertEqual((result["api_requests"], result["metered_model_requests"]), (0, 0))
        self.assertFalse(result["knowledge_written"])
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())

    def test_record_is_idempotent_and_retrievable(self):
        expected = assess_testing_scope_claim(
            self.root, self.goal_id, self.path)["source_artifact_sha256"]
        result = record_testing_scope_claim(
            self.root, self.goal_id, self.path, expected_artifact_sha256=expected)
        self.assertEqual(result["status"], "consolidated")
        self.assertEqual(result["new_evidence_count"], 2)
        self.assertEqual(result["decision"]["host_count"], 2)
        second = record_testing_scope_claim(
            self.root, self.goal_id, self.path, expected_artifact_sha256=expected)
        self.assertEqual(second["new_evidence_count"], 0)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["knowledge_evidence"], 2)
        context = knowledge_context_for_query(self.root, "penetration testing scope and authorization")
        self.assertTrue(any(row["claim"] == CLAIM for row in context["results"]))

    def test_missing_clause_cannot_be_recorded(self):
        self.pages[1]["text"] = "A vulnerability policy lists reporting contacts. " * 3
        self.save()
        report = assess_testing_scope_claim(self.root, self.goal_id, self.path)
        self.assertEqual(report["status"], "insufficient_joint_support")
        with self.assertRaises(ValueError):
            record_testing_scope_claim(
                self.root, self.goal_id, self.path,
                expected_artifact_sha256=report["source_artifact_sha256"])
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())

    def test_wrong_host_url_or_text_digest_is_rejected(self):
        self.pages[0]["host"] = "not-owasp.example"
        self.save()
        with self.assertRaises(ValueError):
            assess_testing_scope_claim(self.root, self.goal_id, self.path)
        self.pages[0]["host"] = "cheatsheetseries.owasp.org"
        self.pages[1]["url"] = "https://example.org/false-policy"
        self.save()
        with self.assertRaises(ValueError):
            assess_testing_scope_claim(self.root, self.goal_id, self.path)
        self.pages[1]["url"] = ("https://www.cisa.gov/news-events/news/"
                                "cisa-issues-final-vulnerability-disclosure-policy-directive-federal-agencies")
        self.save()
        saved = json.loads(self.path.read_text())
        saved["documents"][1]["text"] += " altered"
        self.path.write_text(json.dumps(saved))
        with self.assertRaises(ValueError):
            assess_testing_scope_claim(self.root, self.goal_id, self.path)

    def test_paused_goal_changed_artifact_or_running_runtime_blocks_record(self):
        expected = assess_testing_scope_claim(
            self.root, self.goal_id, self.path)["source_artifact_sha256"]
        saved = json.loads(self.path.read_text())
        saved["metadata_evidence"] = [{"changed": True}]
        self.path.write_text(json.dumps(saved))
        with self.assertRaises(ValueError):
            record_testing_scope_claim(
                self.root, self.goal_id, self.path, expected_artifact_sha256=expected)
        self.save()
        with patch("sira.learning_goal_scope_claim.RuntimeStateStore.status",
                   return_value={"desired_state": "on", "worker_alive": True,
                                 "state_health": "ok"}):
            with self.assertRaises(ValueError):
                record_testing_scope_claim(
                    self.root, self.goal_id, self.path,
                    expected_artifact_sha256=expected)
        self.goals.set_status(self.goal_id, "paused")
        with self.assertRaises(ValueError):
            record_testing_scope_claim(
                self.root, self.goal_id, self.path, expected_artifact_sha256=expected)
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())

    def test_symlink_and_single_source_artifacts_are_rejected(self):
        link = self.path.parent / f"{self.goal_id}_{'b' * 32}.json"
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            assess_testing_scope_claim(self.root, self.goal_id, link)
        saved = json.loads(self.path.read_text())
        saved["documents"].pop()
        self.path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaises(ValueError):
            assess_testing_scope_claim(self.root, self.goal_id, self.path)


if __name__ == "__main__":
    unittest.main()
