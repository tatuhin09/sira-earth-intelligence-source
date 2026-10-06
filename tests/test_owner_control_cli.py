from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SRC_TEXT = str(SRC)
sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]

from sira.access_requests import AccessNeed, AccessRequestStore, KIND_CREDENTIAL
from sira.cli import main
from sira.owner_notifications import OwnerNotificationStore


class OwnerControlCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args: str):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--root", str(self.root), *args])
        return code, json.loads(out.getvalue())

    def _request(self):
        return AccessRequestStore(self.root).create(
            AccessNeed(
                kind=KIND_CREDENTIAL,
                resource="tavily",
                reason="Web search requires provider access.",
                provider_id="tavily",
                credential_name="TAVILY_API_KEY",
                owner_action="Provision the credential through the approved secret channel.",
            ),
            task_kind="autonomous_target",
            task_id="opportunity:op_" + "a" * 32,
        )

    def test_access_list_is_json_and_empty_by_default(self):
        code, payload = self._run("access", "list")
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema"], "sira.access_requests.v1")
        self.assertEqual(payload["request_count"], 0)

    def test_access_approve_then_satisfy_lifecycle(self):
        row = self._request()

        code, approved = self._run("access", "approve", row["request_id"])
        self.assertEqual(code, 0)
        self.assertEqual(approved["status"], "approved_waiting_provision")

        (self.root / ".env").write_text("TAVILY_API_KEY=owner-cli-test-key\n", encoding="utf-8")
        code, satisfied = self._run("access", "satisfy", row["request_id"])
        self.assertEqual(code, 0)
        self.assertEqual(satisfied["status"], "satisfied_resume_ready")

        code, ready = self._run("access", "resume-ready")
        self.assertEqual(code, 0)
        self.assertEqual(len(ready["requests"]), 1)
        self.assertEqual(ready["requests"][0]["request_id"], row["request_id"])

    def test_access_satisfy_without_local_credential_stays_approved(self):
        row = self._request()
        self._run("access", "approve", row["request_id"])
        code, result = self._run("access", "satisfy", row["request_id"])
        self.assertEqual(code, 0)
        self.assertFalse(result["satisfied"])
        self.assertEqual(result["status"], "approved_waiting_provision")
        self.assertEqual(result["verification"]["reason"], "credential_missing")

    def test_access_deny_removes_pending_owner_action(self):
        row = self._request()
        code, denied = self._run("access", "deny", row["request_id"])
        self.assertEqual(code, 0)
        self.assertEqual(denied["status"], "denied")

        code, payload = self._run("access", "list")
        self.assertEqual(code, 0)
        self.assertEqual(payload["pending_count"], 0)

    def test_notifications_list_uses_durable_outbox(self):
        self._request()
        code, payload = self._run("notifications", "list")
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema"], "sira.owner_notifications.v1")
        self.assertEqual(payload["event_count"], 1)
        self.assertEqual(payload["pending_count"], 1)

    def test_notification_ack_uses_existing_delivery_state(self):
        self._request()
        store = OwnerNotificationStore(self.root)
        store.reconcile_access_requests()
        event = store.iter_events()[0]
        store.mark_delivery(
            event["notification_id"],
            ok=True,
            now_epoch=1000,
        )

        code, payload = self._run(
            "notifications",
            "ack",
            event["notification_id"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "acknowledged")

    def test_access_show_returns_one_request(self):
        row = self._request()
        code, payload = self._run("access", "show", row["request_id"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["request_id"], row["request_id"])


if __name__ == "__main__":
    unittest.main()
