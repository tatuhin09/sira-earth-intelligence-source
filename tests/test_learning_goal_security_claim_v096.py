"""A two-part security claim needs exact, independent official evidence."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.knowledge_runtime import knowledge_context_for_query
from sira.learning_goal_security_claim import (
    CLAIM, assess_security_risk_claim, record_security_risk_claim,
)
from sira.learning_goals import LearningGoalStore


class SecurityRiskClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.goals = LearningGoalStore(self.root)
        self.goal_id = self.goals.create(
            "Cybersecurity fundamentals and defense", public_research_allowed=True,
        )["goal_id"]
        self.focus = "Cybersecurity risk assessment and threat modeling"
        self.goals.set_plan(self.goal_id, [self.focus, "Network security controls and segmentation"])
        self.path = (self.root / "memory" / "learning_goal_documents" /
                     f"{self.goal_id}_{'a' * 32}.json")
        self.pages = [
            {
                "url": "https://www.cisa.gov/resources-tools/resources/risk-assessment-methodologies",
                "host": "www.cisa.gov", "title": "Risk Assessment Methodologies",
                "text": ("Risk assessment involves the evaluation of risks taking into consideration "
                         "the potential direct and indirect consequences of an incident, "
                         "known vulnerabilities to various potential hazards. Other methods exist."),
            },
            {
                "url": "https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html",
                "host": "cheatsheetseries.owasp.org", "title": "Threat Modeling Cheat Sheet",
                "text": ("Threat modeling seeks to identify potential security issues during the design phase. "
                         "This allows security to be built into a system. "
                         "The model also considers how threats may affect it."),
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
            "learning_goal_id": self.goal_id, "topic": "Cybersecurity fundamentals and defense",
            "research_query": self.focus, "status": "quotes_need_claim_verification",
            "documents": documents, "metadata_evidence": [], "source_failures": [],
            "verified_claims_recorded": False,
        }), encoding="utf-8")

    def test_assessment_is_read_only_and_checks_both_components(self):
        report = assess_security_risk_claim(self.root, self.goal_id, self.path)
        self.assertEqual(report["status"], "joint_source_supported")
        self.assertEqual(report["claim"], CLAIM)
        self.assertEqual([x["host"] for x in report["support"]],
                         ["www.cisa.gov", "cheatsheetseries.owasp.org"])
        self.assertEqual({x["component"] for x in report["support"]},
                         {"risk_evaluation", "design_security_issues"})
        self.assertEqual((report["api_requests"], report["metered_model_requests"]), (0, 0))
        self.assertFalse(report["knowledge_written"])
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())

    def test_record_is_idempotent_and_retrievable(self):
        expected = assess_security_risk_claim(self.root, self.goal_id, self.path)["source_artifact_sha256"]
        first = record_security_risk_claim(
            self.root, self.goal_id, self.path, expected_artifact_sha256=expected)
        self.assertEqual(first["status"], "consolidated")
        self.assertEqual(first["new_evidence_count"], 2)
        self.assertEqual(first["decision"]["host_count"], 2)
        self.assertEqual(record_security_risk_claim(
            self.root, self.goal_id, self.path,
            expected_artifact_sha256=expected)["new_evidence_count"], 0)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["knowledge_evidence"], 2)
        context = knowledge_context_for_query(self.root, "risk assessment threat modeling")
        self.assertTrue(any(x["claim"] == CLAIM for x in context["results"]))

    def test_one_component_missing_cannot_be_recorded(self):
        self.pages[1]["text"] = ("Threat modeling is used during the design phase. "
                                  "Teams can discuss their design and update it later. "
                                  "This page contains no explicit supported proposition here.")
        self.save()
        report = assess_security_risk_claim(self.root, self.goal_id, self.path)
        self.assertEqual(report["status"], "insufficient_joint_support")
        self.assertEqual([x["component"] for x in report["support"]], ["risk_evaluation"])
        with self.assertRaises(ValueError):
            record_security_risk_claim(
                self.root, self.goal_id, self.path,
                expected_artifact_sha256=report["source_artifact_sha256"])
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())

    def test_changed_text_digest_and_host_are_rejected(self):
        saved = json.loads(self.path.read_text())
        saved["documents"][0]["text"] += " altered"
        self.path.write_text(json.dumps(saved))
        with self.assertRaises(ValueError):
            record_security_risk_claim(
                self.root, self.goal_id, self.path,
                expected_artifact_sha256="0" * 64)
        self.save()
        self.pages[0]["url"] = "https://example.com/resources-tools/resources/risk-assessment-methodologies"
        self.save()
        with self.assertRaises(ValueError):
            assess_security_risk_claim(self.root, self.goal_id, self.path)
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())

    def test_paused_goal_and_unexpected_artifact_path_are_rejected(self):
        other = self.path.parent / f"{self.goal_id}_{'b' * 32}.json"
        other.symlink_to(self.path)
        with self.assertRaises(ValueError):
            assess_security_risk_claim(self.root, self.goal_id, other)
        self.goals.set_status(self.goal_id, "paused")
        with self.assertRaises(ValueError):
            record_security_risk_claim(
                self.root, self.goal_id, self.path,
                expected_artifact_sha256="0" * 64)
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())

    def test_replaced_artifact_after_dry_assessment_cannot_be_recorded(self):
        expected = assess_security_risk_claim(self.root, self.goal_id, self.path)["source_artifact_sha256"]
        saved = json.loads(self.path.read_text())
        saved["metadata_evidence"] = [{"note": "changed after review"}]
        self.path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaises(ValueError):
            record_security_risk_claim(
                self.root, self.goal_id, self.path,
                expected_artifact_sha256=expected)
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
