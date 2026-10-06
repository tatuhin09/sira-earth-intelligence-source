from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.desktop_app import (
    DesktopChatStore,
    DesktopControl,
    _memory_summary,
)
from sira.runtime import RuntimeStateStore
from sira.self_modification import PROTECTED_PATHS
from sira.storage import write_json


class DesktopAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        RuntimeStateStore(self.root).save({
            "desired_state": "off",
            "worker_state": "stopped",
            "pid": None,
            "started_at": "2026-09-21T10:00:00+00:00",
            "heartbeat_at": "2026-09-21T10:00:00+00:00",
            "generation": 4,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_desktop_control_module_is_protected(self):
        self.assertIn(
            "src/sira/desktop_app.py",
            PROTECTED_PATHS,
        )

    def test_overview_is_read_only_and_reports_runtime(self):
        with patch(
            "sira.desktop_app._git_summary",
            return_value={
                "available": True,
                "revision": "fixture",
                "clean": True,
            },
        ):
            report = DesktopControl(self.root).overview()
        self.assertEqual(
            report["runtime"]["effective_state"],
            "stopped",
        )
        self.assertEqual(
            report["runtime"]["desired_state"],
            "off",
        )
        self.assertEqual(
            report["git"]["revision"],
            "fixture",
        )

    def test_chat_status_reply_is_local_and_non_authoritative(self):
        control = DesktopControl(self.root)
        reply = control.chat_send("what is your status?")
        self.assertIn(
            "stopped",
            reply["assistant"]["text"],
        )
        history = control.chat_history()["messages"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[1]["role"], "assistant")

    def test_chat_cannot_start_runtime_from_free_form_text(self):
        control = DesktopControl(self.root)
        reply = control.chat_send("start SIRA now")
        self.assertIn(
            "explicit Start SIRA",
            reply["assistant"]["text"],
        )
        status = RuntimeStateStore(self.root).status()
        self.assertEqual(status["desired_state"], "off")

    def test_runtime_start_stop_delegate_to_existing_controller(self):
        control = DesktopControl(self.root)
        with patch(
            "sira.desktop_app.AutonomousRuntime.start",
            return_value={"status": "started"},
        ) as start:
            self.assertEqual(
                control.runtime_start()["status"],
                "started",
            )
        start.assert_called_once()
        with patch(
            "sira.desktop_app.AutonomousRuntime.stop",
            return_value={"status": "stopped"},
        ) as stop:
            self.assertEqual(
                control.runtime_stop()["status"],
                "stopped",
            )
        stop.assert_called_once()

    def test_chat_store_rejects_oversized_text(self):
        store = DesktopChatStore(self.root)
        with self.assertRaises(ValueError):
            store.append("user", "x" * 4001)


if __name__ == "__main__":
    unittest.main()
