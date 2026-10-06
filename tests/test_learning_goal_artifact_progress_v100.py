"""Goal detail attributes verified local knowledge to the saved study focus."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.desktop_app import DesktopControl
from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goal_security_claim import record_security_risk_claim
from sira.learning_goals import LearningGoalStore


class LearningGoalArtifactProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal_id = self.store.create(
            "Cybersecurity fundamentals and defense", public_research_allowed=True
        )["goal_id"]
        self.focus = "Cybersecurity risk assessment and threat modeling"
        self.store.set_plan(self.goal_id, [self.focus, "Network security controls and segmentation"])
        self.artifact = (self.root / "memory/learning_goal_documents" /
                         f"{self.goal_id}_{'a' * 32}.json")
        self.documents = [
            {
                "url": "https://www.cisa.gov/resources-tools/resources/risk-assessment-methodologies",
                "host": "www.cisa.gov", "title": "Risk Assessment Methodologies",
                "text": ("Risk assessment involves the evaluation of risks taking into consideration "
                         "the potential direct and indirect consequences of an incident, "
                         "known vulnerabilities to various potential hazards."),
            },
            {
                "url": "https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html",
                "host": "cheatsheetseries.owasp.org", "title": "Threat Modeling Cheat Sheet",
                "text": ("Threat modeling seeks to identify potential security issues during the "
                         "design phase. It helps teams assess their security design."),
            },
        ]
        self.save()

    def save(self, *, focus=None):
        self.artifact.parent.mkdir(parents=True, exist_ok=True)
        documents = [
            {**row, "content_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
             "retrieved_at": "2026-09-27T10:00:00+00:00", "verified": False}
            for row in self.documents
        ]
        self.artifact.write_text(json.dumps({
            "schema": "sira.learning_goal_general_documents.v1",
            "learning_goal_id": self.goal_id,
            "topic": "Cybersecurity fundamentals and defense",
            "research_query": self.focus if focus is None else focus,
            "status": "quotes_need_claim_verification",
            "documents": documents, "metadata_evidence": [], "source_failures": [],
            "verified_claims_recorded": False,
        }), encoding="utf-8")

    def record(self):
        digest = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        return record_security_risk_claim(
            self.root, self.goal_id, self.artifact, expected_artifact_sha256=digest
        )

    def show(self):
        with patch("sira.desktop_app.run_desktop_model_chat",
                   side_effect=AssertionError("Model must not run")):
            return DesktopControl(self.root).chat_send(f"Goal show {self.goal_id}")["assistant"]["text"]

    def test_verified_artifact_counts_for_exact_step_without_mutating_attempt_history(self):
        self.record()
        text = self.show()
        self.assertIn("Progress: 1 of 2 steps have some verified claim evidence", text)
        self.assertIn(self.focus + ": some verified local evidence (0 attempts)", text)
        self.assertIn("Network security controls and segmentation: not attempted", text)
        goal = self.store.get(self.goal_id)
        self.assertEqual(goal["study_step_stats"][0]["verified_claim_events"], 0)
        self.assertEqual(len(goal["study_history"]), 0)

    def test_modified_artifact_does_not_count_as_study_step_evidence(self):
        self.record()
        saved = json.loads(self.artifact.read_text())
        saved["documents"][0]["text"] += " altered"
        self.artifact.write_text(json.dumps(saved), encoding="utf-8")
        text = self.show()
        self.assertIn("Progress: 0 of 2 steps", text)
        self.assertIn(self.focus + ": not attempted", text)

    def test_saved_artifact_from_unrelated_focus_cannot_count(self):
        self.record()
        other = self.store.create("Python software testing", public_research_allowed=True)
        self.store.set_plan(other["goal_id"], [self.focus, "Python fixtures"])
        with patch("sira.desktop_app.run_desktop_model_chat",
                   side_effect=AssertionError("Model must not run")):
            text = DesktopControl(self.root).chat_send(
                "Goal show " + other["goal_id"]
            )["assistant"]["text"]
        self.assertIn("Progress: 0 of 2 steps", text)

    def test_verified_step_remains_visible_after_many_unverified_reports(self):
        # A goal detail opened before verification must not freeze an empty
        # index that prevents discovering the earlier source later.
        KnowledgeConsolidationStore(self.root)
        self.assertIn("Progress: 0 of 2 steps", self.show())
        self.record()
        older = self.artifact.stat().st_mtime + 1
        saved = json.loads(self.artifact.read_text(encoding="utf-8"))
        for number in range(33):
            path = self.artifact.with_name(
                f"{self.goal_id}_{number:032x}.json"
            )
            noise = json.loads(json.dumps(saved))
            noise["documents"][0]["text"] += f" Another unverified detail {number}."
            noise["documents"][0]["content_sha256"] = hashlib.sha256(
                noise["documents"][0]["text"].encode("utf-8")
            ).hexdigest()
            path.write_text(json.dumps(noise), encoding="utf-8")
            os.utime(path, (older + number, older + number))
        text = self.show()
        self.assertIn("Progress: 1 of 2 steps have some verified claim evidence", text)
        self.assertIn(self.focus + ": some verified local evidence", text)
        index = self.root / "memory/learning_goal_progress" / f"{self.goal_id}.json"
        self.assertTrue(index.is_file())
        for number in range(33, 66):
            path = self.artifact.with_name(f"{self.goal_id}_{number:032x}.json")
            path.write_text(json.dumps({"unverified": True}), encoding="utf-8")
            os.utime(path, (older + number, older + number))
        self.assertIn("Progress: 1 of 2 steps", self.show())

        # The index cannot keep progress alive if the original source bytes
        # have changed after verification.
        original = json.loads(self.artifact.read_text(encoding="utf-8"))
        original["documents"][0]["text"] += " altered"
        self.artifact.write_text(json.dumps(original), encoding="utf-8")
        self.assertIn("Progress: 0 of 2 steps", self.show())


if __name__ == "__main__":
    unittest.main()
