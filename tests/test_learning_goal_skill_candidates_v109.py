"""Verified goal knowledge may suggest practice; it cannot prove a skill."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.desktop_app import DesktopControl
from sira.learning_consolidation import LearningConsolidationStore
from sira.learning_goal_security_claim import record_security_risk_claim
from sira.learning_goal_skill_candidates import advisory_skill_candidates
from sira.learning_goals import LearningGoalStore


class SkillCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create("Cybersecurity fundamentals and defense",
                                      public_research_allowed=True)
        self.focus = "Cybersecurity risk assessment and threat modeling"
        self.store.set_plan(self.goal["goal_id"],
                            [self.focus, "Network security controls and segmentation"])
        self.artifact = (self.root / "memory/learning_goal_documents" /
                         f"{self.goal['goal_id']}_{'a' * 32}.json")
        self.artifact.parent.mkdir(parents=True, exist_ok=True)
        documents = [
            ("https://www.cisa.gov/resources-tools/resources/risk-assessment-methodologies",
             "www.cisa.gov", "Risk Assessment Methodologies",
             "Risk assessment involves the evaluation of risks taking into consideration "
             "the potential direct and indirect consequences of an incident, "
             "known vulnerabilities to various potential hazards."),
            ("https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html",
             "cheatsheetseries.owasp.org", "Threat Modeling Cheat Sheet",
             "Threat modeling seeks to identify potential security issues during the "
             "design phase. It helps teams assess their security design."),
        ]
        self.artifact.write_text(json.dumps({
            "schema": "sira.learning_goal_general_documents.v1",
            "learning_goal_id": self.goal["goal_id"], "topic": self.goal["topic"],
            "research_query": self.focus, "status": "quotes_need_claim_verification",
            "documents": [
                {"url": url, "host": host, "title": title, "text": text,
                 "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                 "retrieved_at": "2026-09-27T10:00:00+00:00", "verified": False}
                for url, host, title, text in documents
            ], "metadata_evidence": [], "source_failures": [],
            "verified_claims_recorded": False,
        }), encoding="utf-8")

    def candidates(self):
        return advisory_skill_candidates(self.root, self.store.get(self.goal["goal_id"]))

    def test_only_rechecked_two_host_claim_creates_non_executable_candidate(self):
        self.assertEqual(self.candidates(), [])
        digest = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        result = record_security_risk_claim(
            self.root, self.goal["goal_id"], self.artifact,
            expected_artifact_sha256=digest,
        )
        candidates = self.candidates()
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["focus"], self.focus)
        self.assertEqual(candidate["claim"], result["claim"])
        self.assertEqual(len(candidate["source_urls"]), 2)
        self.assertEqual(candidate["status"], "needs_demonstrated_application")
        self.assertTrue(candidate["executable"])
        self.assertEqual(candidate["execution_mode"], "isolated_evidence_trace_v1")
        self.assertFalse(candidate["authority_granted"])
        self.assertFalse(candidate["skill_activated"])
        self.assertEqual(self.candidates(), candidates)  # Stable, no duplicate proposal.
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])

        with patch("sira.desktop_app.run_desktop_model_chat",
                   side_effect=AssertionError("Unexpected model call")):
            shown = DesktopControl(self.root).chat_send("Goal show " + self.goal["goal_id"])
        self.assertIn("Practice candidates (not demonstrated skills): 1", shown["assistant"]["text"])
        self.assertIn(self.focus, shown["assistant"]["text"])
        self.assertIsNone(shown["model"])

    def test_tampered_source_removes_candidate_without_leaving_a_skill(self):
        digest = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        record_security_risk_claim(self.root, self.goal["goal_id"], self.artifact,
                                   expected_artifact_sha256=digest)
        self.assertEqual(len(self.candidates()), 1)
        saved = json.loads(self.artifact.read_text(encoding="utf-8"))
        saved["documents"][0]["text"] += " tampered"
        self.artifact.write_text(json.dumps(saved), encoding="utf-8")
        self.assertEqual(self.candidates(), [])
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])

    def test_matching_goal_title_without_its_own_artifact_is_not_enough(self):
        digest = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        record_security_risk_claim(self.root, self.goal["goal_id"], self.artifact,
                                   expected_artifact_sha256=digest)
        other = self.store.create("Other cybersecurity study", public_research_allowed=True)
        self.store.set_plan(other["goal_id"], [self.focus, "Incident response"])
        self.assertEqual(advisory_skill_candidates(
            self.root, self.store.get(other["goal_id"])), [])


if __name__ == "__main__":
    unittest.main()
