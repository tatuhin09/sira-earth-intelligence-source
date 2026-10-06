"""Goal detail shows current verified knowledge alongside attempt history."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.desktop_app import DesktopControl
from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goals import LearningGoalStore
from sira.models import utc_now


CLAIM = ("Risk assessment evaluates risks and considers incident consequences, "
         "while threat modeling identifies potential security issues during design.")


class DesktopLearningGoalLocalKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        store = LearningGoalStore(self.root)
        self.goal_id = store.create(
            "Cybersecurity fundamentals and defense", public_research_allowed=True,
        )["goal_id"]
        store.set_plan(self.goal_id, [
            "Cybersecurity risk assessment and threat modeling",
            "Identity and access management",
        ])

    def tearDown(self):
        self.temp.cleanup()

    def add_claim(self, *, two_hosts=True, claim=CLAIM):
        store = KnowledgeConsolidationStore(self.root)
        sources = [
            ("S1", "https://www.cisa.gov/resources-tools/resources/risk-assessment-methodologies"),
            ("S2", "https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html"),
        ]
        for source_id, url in sources if two_hosts else sources[:1]:
            store.record_evidence(
                "claim.security.example", claim,
                evidence_id="evidence_" + source_id, source_id=source_id,
                source_url=url, confidence=.85, verifier_kind="verified_official_quote",
                evidence_sha256="a" * 64, retrieved_at=utc_now(), verified=True,
            )
        store.consolidate("claim.security.example")

    def show(self):
        with patch("sira.desktop_app.run_desktop_model_chat",
                   side_effect=AssertionError("Unexpected model call")):
            return DesktopControl(self.root).chat_send(f"Goal show {self.goal_id}")

    def test_verified_local_claim_is_visible_without_inflating_attempt_progress(self):
        self.add_claim()
        reply = self.show()
        self.assertEqual(reply["mode"], "local_learning_goals")
        self.assertIsNone(reply["model"])
        text = reply["assistant"]["text"]
        self.assertIn("0 of 2 steps", text)
        self.assertIn("Relevant verified local claims: 1", text)
        self.assertIn(CLAIM, text)
        self.assertIn("not full mastery", text)
        self.assertEqual(len(LearningGoalStore(self.root).get(self.goal_id).get("study_history") or []), 0)

    def test_pending_one_source_claim_is_not_shown(self):
        self.add_claim(two_hosts=False)
        text = self.show()["assistant"]["text"]
        self.assertNotIn("Relevant verified local claims", text)
        self.assertNotIn(CLAIM, text)

    def test_unrelated_verified_claim_is_not_shown(self):
        self.add_claim(claim="Python unittest and pytest both support test discovery.")
        text = self.show()["assistant"]["text"]
        self.assertNotIn("Relevant verified local claims", text)
        self.assertNotIn("Python unittest", text)


if __name__ == "__main__":
    unittest.main()
