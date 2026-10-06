"""Desktop chat can save deliberate owner learning requests locally."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.desktop_app import DesktopControl
from sira.learning_goals import LearningGoalStore
from sira.runtime import RuntimeStateStore


class DesktopLearningGoalsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_goal_is_persisted_and_visible_after_chat_restart(self):
        with patch("sira.desktop_app.run_desktop_model_chat") as model:
            reply = DesktopControl(self.root).chat_send(
                "Goal: Cybersecurity fundamentals | priority=high | public=yes"
            )
        goal = LearningGoalStore(self.root).list()[0]
        self.assertEqual(reply["mode"], "local_learning_goal")
        self.assertIn(goal["goal_id"], reply["assistant"]["text"])
        self.assertEqual(goal["topic"], "Cybersecurity fundamentals")
        self.assertEqual(goal["priority"], "high")
        self.assertTrue(goal["public_research_allowed"])
        self.assertEqual(goal["status"], "active")
        self.assertEqual(DesktopControl(self.root).chat_history()["messages"][-1]["role"], "assistant")
        self.assertNotEqual(RuntimeStateStore(self.root).status()["desired_state"], "on")
        model.assert_not_called()

    def test_daily_learning_request_is_local_only_and_duplicate_is_idempotent(self):
        control = DesktopControl(self.root)
        with patch("sira.desktop_app.run_desktop_model_chat") as model:
            first = control.chat_send("SIRA, learn about Python software testing every day")
            second = control.chat_send("SIRA, learn about Python software testing every day")
        rows = LearningGoalStore(self.root).list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["topic"], "Python software testing")
        self.assertFalse(rows[0]["public_research_allowed"])
        self.assertIn(rows[0]["goal_id"], first["assistant"]["text"])
        self.assertIn(rows[0]["goal_id"], second["assistant"]["text"])
        model.assert_not_called()

    def test_unambiguous_bangla_daily_goal_is_stored_without_external_permission(self):
        with patch("sira.desktop_app.run_desktop_model_chat") as model:
            reply = DesktopControl(self.root).chat_send("শেখো নেটওয়ার্ক নিরাপত্তা প্রতিদিন")
        self.assertEqual(reply["mode"], "local_learning_goal")
        self.assertEqual(LearningGoalStore(self.root).list()[0]["topic"], "নেটওয়ার্ক নিরাপত্তা")
        model.assert_not_called()

    def test_question_and_one_time_research_are_not_silently_saved_as_goals(self):
        fake = {"status": "completed", "reply": "A guarded answer.",
                "intent": "conversation", "needs_research": False,
                "access_request_id": None, "metrics": {"api_requests": 1}, "artifact": None}
        with patch("sira.desktop_app.run_desktop_model_chat", return_value=fake):
            reply = DesktopControl(self.root).chat_send("How do I learn Python?")
        self.assertEqual(reply["mode"], "model")
        with patch.object(DesktopControl, "start_research", return_value={"status": "started"}) as research:
            result = DesktopControl(self.root).chat_send("Research Python software testing")
        self.assertEqual(result["status"], "started")
        research.assert_called_once()
        self.assertEqual(LearningGoalStore(self.root).list(), [])

    def test_bad_options_or_conflicting_existing_policy_are_reported_without_new_goal(self):
        control = DesktopControl(self.root)
        with patch("sira.desktop_app.run_desktop_model_chat") as model:
            invalid = control.chat_send("Goal: Incident response | public=maybe")
            first = control.chat_send("Goal: Incident response")
            conflict = control.chat_send("Goal: Incident response | public=yes")
        rows = LearningGoalStore(self.root).list()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["public_research_allowed"])
        self.assertIn("invalid", invalid["assistant"]["text"].casefold())
        self.assertIn("existing", conflict["assistant"]["text"].casefold())
        self.assertEqual(invalid["mode"], "local_learning_goal_error")
        self.assertEqual(conflict["mode"], "local_learning_goal_error")
        self.assertEqual(first["mode"], "local_learning_goal")
        model.assert_not_called()

    def test_explicit_research_button_keeps_one_time_research_semantics(self):
        with patch.object(DesktopControl, "start_research", return_value={"status": "started"}) as research:
            result = DesktopControl(self.root).chat_send("Goal: Rust", research=True)
        self.assertEqual(result["status"], "started")
        research.assert_called_once_with("Goal: Rust")
        self.assertEqual(LearningGoalStore(self.root).list(), [])

    def test_rejected_oversized_chat_cannot_write_a_goal_first(self):
        with self.assertRaises(ValueError):
            DesktopControl(self.root).chat_send("Goal: Rust | priority=" + " " * 4001 + "normal")
        self.assertEqual(LearningGoalStore(self.root).list(), [])

    def test_existing_paused_goal_is_not_described_as_scheduled(self):
        store = LearningGoalStore(self.root)
        goal = store.create("Rust", priority="normal")
        store.set_status(goal["goal_id"], "paused")
        reply = DesktopControl(self.root).chat_send("Goal: Rust")
        self.assertIn("paused", reply["assistant"]["text"].casefold())
        self.assertEqual(store.get(goal["goal_id"])["status"], "paused")


if __name__ == "__main__":
    unittest.main()
