"""Owner can inspect and plan durable learning goals through local chat."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.desktop_app import DesktopControl
from sira.learning_goals import LearningGoalStore


class DesktopLearningGoalManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create("Cybersecurity fundamentals", priority="high")

    def tearDown(self):
        self.temp.cleanup()

    def test_list_and_show_are_local_and_report_durable_status(self):
        self.store.set_status(self.goal["goal_id"], "paused")
        with patch("sira.desktop_app.run_desktop_model_chat") as model:
            listing = DesktopControl(self.root).chat_send("Goals list")
            detail = DesktopControl(self.root).chat_send(f"Goal show {self.goal['goal_id']}")
        self.assertEqual(listing["mode"], "local_learning_goals")
        self.assertIn(self.goal["goal_id"], listing["assistant"]["text"])
        self.assertIn("paused", detail["assistant"]["text"])
        self.assertEqual(self.store.get(self.goal["goal_id"])["status"], "paused")
        model.assert_not_called()

    def test_plan_is_saved_idempotently_without_resetting_retry(self):
        goal_id = self.goal["goal_id"]
        self.store.record_attempt(goal_id, "research_sources_insufficient", at_epoch=123)
        command = f"Goal plan {goal_id}: Network basics | Defensive monitoring | Incident response"
        with patch("sira.desktop_app.run_desktop_model_chat") as model:
            first = DesktopControl(self.root).chat_send(command)
            second = DesktopControl(self.root).chat_send(command)
            detail = DesktopControl(self.root).chat_send(f"Goal show {goal_id}")
        row = self.store.get(goal_id)
        self.assertEqual(first["mode"], "local_learning_goal")
        self.assertEqual(second["mode"], "local_learning_goal")
        self.assertEqual(row["study_plan"], ["Network basics", "Defensive monitoring", "Incident response"])
        self.assertEqual(row["study_next_index"], 0)
        self.assertEqual(row["last_attempt_at_epoch"], 123)
        self.assertIn("Network basics", detail["assistant"]["text"])
        model.assert_not_called()

    def test_invalid_or_replacement_plan_does_not_silently_change_saved_plan(self):
        goal_id = self.goal["goal_id"]
        with patch("sira.desktop_app.run_desktop_model_chat") as model:
            invalid = DesktopControl(self.root).chat_send(f"Goal plan {goal_id}: Only one step")
            valid = DesktopControl(self.root).chat_send(f"Goal plan {goal_id}: One | Two")
            replace = DesktopControl(self.root).chat_send(f"Goal plan {goal_id}: Three | Four")
            unknown = DesktopControl(self.root).chat_send("Goal show lg_" + "f" * 32)
        self.assertEqual(invalid["mode"], "local_learning_goal_error")
        self.assertEqual(valid["mode"], "local_learning_goal")
        self.assertEqual(replace["mode"], "local_learning_goal_error")
        self.assertEqual(unknown["mode"], "local_learning_goal_error")
        self.assertEqual(self.store.get(goal_id)["study_plan"], ["One", "Two"])
        model.assert_not_called()

    def test_explicit_pause_and_resume_use_existing_store(self):
        goal_id = self.goal["goal_id"]
        with patch("sira.desktop_app.run_desktop_model_chat") as model:
            paused = DesktopControl(self.root).chat_send(f"Goal pause {goal_id}")
            self.assertEqual(self.store.get(goal_id)["status"], "paused")
            resumed = DesktopControl(self.root).chat_send(f"Goal resume {goal_id}")
        self.assertIn("paused", paused["assistant"]["text"])
        self.assertIn("active", resumed["assistant"]["text"])
        self.assertEqual(self.store.get(goal_id)["status"], "active")
        model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
