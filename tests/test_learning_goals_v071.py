from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.cli import main
from sira.learning_goals import LearningGoalStore


class LearningGoalsV071Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--root", str(self.root), "goals", *args])
        return result, json.loads(output.getvalue())

    def test_owner_topic_survives_restart_and_can_pause_and_resume(self):
        code, added = self.cli("add", "শূন্য থেকে কোয়ান্টাম কম্পিউটিং", "--priority", "high")
        self.assertEqual(code, 0)
        self.assertEqual(added["topic"], "শূন্য থেকে কোয়ান্টাম কম্পিউটিং")
        self.assertEqual(added["priority"], "high")
        self.assertEqual(added["status"], "active")
        self.assertFalse(added["public_research_allowed"])
        self.assertEqual(self.cli("list")[1]["goals"], [added])
        goal_id = added["goal_id"]
        self.assertEqual(self.cli("pause", goal_id)[1]["status"], "paused")
        self.assertEqual(self.cli("show", goal_id)[1]["status"], "paused")
        self.assertEqual(self.cli("resume", goal_id)[1]["status"], "active")
        self.assertEqual(self.cli("set-priority", goal_id, "normal")[1]["priority"], "normal")
        self.assertTrue(self.cli("allow-public", goal_id)[1]["public_research_allowed"])
        self.assertFalse(self.cli("local-only", goal_id)[1]["public_research_allowed"])
        self.assertEqual(LearningGoalStore(self.root).get(goal_id)["topic"], added["topic"])

    def test_public_research_requires_explicit_flag_and_duplicate_is_idempotent(self):
        one = self.cli("add", "  Agent  architectures ", "--public-research")[1]
        again = self.cli("add", "agent architectures", "--public-research")[1]
        self.assertEqual(one["goal_id"], again["goal_id"])
        self.assertTrue(again["public_research_allowed"])
        self.assertEqual(len(self.cli("list")[1]["goals"]), 1)
        with self.assertRaises(ValueError):
            LearningGoalStore(self.root).create("agent architectures", public_research_allowed=False)
        with self.assertRaises(ValueError):
            LearningGoalStore(self.root).create("agent architectures", priority="high", public_research_allowed=True)

    def test_corrupt_goal_state_is_not_silently_overwritten(self):
        store = LearningGoalStore(self.root)
        first = store.create("Local verified research")
        path = self.root / "memory" / "learning_goals.json"
        path.write_text('{"schema_version":7}', encoding="utf-8")
        with self.assertRaises(ValueError):
            store.create("Another subject")
        self.assertEqual(path.read_text(encoding="utf-8"), '{"schema_version":7}')
        self.assertNotIn("Another subject", path.read_text(encoding="utf-8"))
        self.assertTrue(first["goal_id"].startswith("lg_"))

    def test_simultaneous_goals_are_not_lost_and_create_does_not_touch_code(self):
        (self.root / "src").mkdir()
        source = self.root / "src" / "test.py"
        source.write_text("unchanged\n", encoding="utf-8")
        with ThreadPoolExecutor(max_workers=6) as pool:
            goals = list(pool.map(lambda n: LearningGoalStore(self.root).create(f"Subject {n}"), range(20)))
        self.assertEqual(len({item["goal_id"] for item in goals}), 20)
        self.assertEqual(len(LearningGoalStore(self.root).list()), 20)
        self.assertEqual(source.read_text(encoding="utf-8"), "unchanged\n")

    def test_empty_topic_and_wrong_goal_id_are_rejected(self):
        store = LearningGoalStore(self.root)
        with self.assertRaises(ValueError):
            store.create("  ")
        with self.assertRaises(ValueError):
            store.set_status("../../secrets", "paused")
        with self.assertRaises(ValueError):
            store.create("Subject", priority=[])


if __name__ == "__main__":
    unittest.main()
