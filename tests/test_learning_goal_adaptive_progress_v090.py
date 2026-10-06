"""Long-running study prioritizes unresolved evidence without declaring mastery."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.desktop_app import DesktopControl
from sira.learning_goals import LearningGoalStore


class LearningGoalAdaptiveProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create("Software testing", public_research_allowed=True)
        self.goal_id = self.goal["goal_id"]
        self.store.set_plan(self.goal_id, ["Test discovery", "Fixtures", "Property testing"])

    def attempt(self, index, verified_count, when):
        self.assertEqual(self.store.get(self.goal_id)["study_next_index"], index)
        outcome = "verified_knowledge_recorded" if verified_count else "research_sources_insufficient"
        return self.store.record_attempt(
            self.goal_id, outcome, at_epoch=when,
            study_step_index=index, verified_claim_count=verified_count,
        )

    def test_unattempted_steps_run_before_revisiting_a_gap(self):
        self.attempt(0, 1, 100)
        self.attempt(1, 0, 200)
        self.attempt(2, 0, 300)
        self.assertEqual(self.store.get(self.goal_id)["study_next_index"], 1)
        self.attempt(1, 0, 400)
        self.assertEqual(self.store.get(self.goal_id)["study_next_index"], 2)

    def test_one_persistent_gap_does_not_starve_other_steps(self):
        self.store = LearningGoalStore(self.root)
        self.attempt(0, 1, 100)
        self.attempt(1, 0, 200)
        self.attempt(2, 1, 300)
        self.assertEqual(self.store.get(self.goal_id)["study_next_index"], 1)
        self.attempt(1, 0, 400)
        self.assertEqual(self.store.get(self.goal_id)["study_next_index"], 0)
        self.attempt(0, 1, 500)
        self.assertEqual(self.store.get(self.goal_id)["study_next_index"], 1)

    def test_progress_survives_bounded_history_truncation(self):
        for number in range(40):
            current = self.store.get(self.goal_id)
            self.store.record_attempt(
                self.goal_id, "research_sources_insufficient", at_epoch=1000 + number,
                study_step_index=current["study_next_index"], verified_claim_count=0,
            )
        saved = LearningGoalStore(self.root).get(self.goal_id)
        self.assertEqual(len(saved["study_history"]), 32)
        self.assertEqual(sum(row["attempt_count"] for row in saved["study_step_stats"]), 40)
        self.assertEqual(sum(row["verified_claim_events"] for row in saved["study_step_stats"]), 0)

    def test_desktop_show_displays_gaps_and_never_calls_them_mastered(self):
        self.attempt(0, 1, 100)
        self.attempt(1, 0, 200)
        with patch("sira.desktop_app.run_desktop_model_chat",
                   side_effect=AssertionError("model must not run")):
            reply = DesktopControl(self.root).chat_send(f"Goal show {self.goal_id}")
        text = reply["assistant"]["text"]
        self.assertEqual(reply["mode"], "local_learning_goals")
        self.assertIn("1 of 3 steps have some verified claim evidence", text)
        self.assertIn("Fixtures: needs evidence", text)
        self.assertIn("Property testing: not attempted", text)
        self.assertNotIn("mastered", text.lower())

    def test_maximum_study_plan_can_still_be_shown_in_desktop_chat(self):
        goal = self.store.create("T" * 500)
        steps = [f"Step {i} " + "x" * 190 for i in range(12)]
        self.store.set_plan(goal["goal_id"], steps)
        with patch("sira.desktop_app.run_desktop_model_chat",
                   side_effect=AssertionError("model must not run")):
            reply = DesktopControl(self.root).chat_send(f"Goal show {goal['goal_id']}")
        self.assertEqual(reply["mode"], "local_learning_goals")
        self.assertLessEqual(len(reply["assistant"]["text"]), 4000)
        self.assertIn("0 of 12 steps", reply["assistant"]["text"])
        for index in range(12):
            self.assertIn(f"Step {index} ", reply["assistant"]["text"])


if __name__ == "__main__":
    unittest.main()
