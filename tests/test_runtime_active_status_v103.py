"""The owner can see current work without mistaking an old cycle for it."""

import json
import os
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.runtime import RuntimeStateStore
from sira.runtime_reliability import ActiveCycleJournal
from sira.storage import write_json


class ActiveRuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RuntimeStateStore(self.root)
        self.store.save({
            "desired_state": "on", "worker_state": "running", "pid": os.getpid(),
            "started_at": "2026-09-27T00:00:00+00:00",
            "heartbeat_at": "2026-09-27T00:00:01+00:00", "generation": 49,
        })

    def test_status_shows_current_selected_goal_and_clears_between_cycles(self):
        journal = ActiveCycleJournal(self.root)
        journal.start(generation=49, cycle_id="sc_" + "a" * 32, phase="selection")
        self.assertIsNotNone(self.store.status().get("active_cycle"))
        self.assertEqual(self.store.status()["active_cycle"]["phase"], "selection")
        self.assertIsNone(self.store.status()["active_cycle"]["target"])

        selection_id = "ats_" + "b" * 32
        write_json(self.root / "improvements/targeting/selections" / (selection_id + ".json"), {
            "schema_version": 1, "kind": "autonomous_target_selection",
            "selection_id": selection_id,
            "target": {
                "target_kind": "learning_goal",
                "learning_goal_id": "lg_" + "c" * 32,
                "topic": "White hat hacker practices and responsible disclosure",
                "secret": "must stay private",
            },
        })
        journal.update("learning_goal_research", target_selection_id=selection_id)
        current = self.store.status()["active_cycle"]
        self.assertEqual(current["phase"], "learning_goal_research")
        self.assertEqual(current["target"], {
            "kind": "learning_goal",
            "learning_goal_id": "lg_" + "c" * 32,
            "topic": "White hat hacker practices and responsible disclosure",
        })
        self.assertNotIn("must stay private", json.dumps(self.store.status()))

        journal.finish(status="completed", outcome="verified_knowledge_recorded")
        self.assertIsNone(self.store.status()["active_cycle"])

    def test_stale_worker_generation_and_unsafe_selection_are_not_current_work(self):
        journal = ActiveCycleJournal(self.root)
        journal.start(generation=48, cycle_id="sc_" + "d" * 32, phase="writer_handoff")
        self.assertIsNone(self.store.status().get("active_cycle"))

        journal.start(generation=49, cycle_id="sc_" + "e" * 32, phase="target_selected")
        selection_id = "ats_" + "f" * 32
        journal.update("target_selected", target_selection_id=selection_id)
        selection_path = self.root / "improvements/targeting/selections" / (selection_id + ".json")
        selection_path.parent.mkdir(parents=True)
        selection_path.symlink_to(self.root / "runtime/self_state.json")
        self.assertIsNotNone(self.store.status().get("active_cycle"))
        self.assertIsNone(self.store.status()["active_cycle"]["target"])

        self.store.save({
            "desired_state": "off", "worker_state": "stopped", "pid": None,
            "started_at": None, "heartbeat_at": None, "generation": 49,
        })
        self.assertIsNone(self.store.status()["active_cycle"])


if __name__ == "__main__":
    unittest.main()
