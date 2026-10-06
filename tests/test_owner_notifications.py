from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SRC_TEXT = str(SRC)
sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]

from sira.access_requests import AccessNeed, AccessRequestStore, KIND_CREDENTIAL
from sira.owner_notifications import (
    OwnerNotificationStore,
    STATUS_ACKNOWLEDGED,
    STATUS_CANCELLED,
    STATUS_DELIVERED,
    STATUS_PENDING,
    pump_owner_notifications,
)


class OwnerNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.access = AccessRequestStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _request(self):
        return self.access.create(
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

    def test_sync_creates_one_deduplicated_outbox_event(self):
        row = self._request()
        store = OwnerNotificationStore(self.root)

        first = store.reconcile_access_requests()
        second = store.reconcile_access_requests()

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        events = store.iter_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source_request_id"], row["request_id"])

    def test_outbox_contains_access_metadata_but_no_secret_value(self):
        self._request()
        store = OwnerNotificationStore(self.root)
        store.reconcile_access_requests()
        event = store.iter_events()[0]

        self.assertEqual(event["status"], STATUS_PENDING)
        self.assertFalse(event["contains_secret_value"])
        self.assertIn("tavily", event["title"].lower())
        self.assertNotIn("sk-", event["body"])

    def test_successful_delivery_marks_delivered_and_is_not_resent(self):
        self._request()
        calls = []

        def sender(event):
            calls.append(event["notification_id"])
            return {"ok": True}

        first = pump_owner_notifications(self.root, sender=sender, now_epoch=1000)
        second = pump_owner_notifications(self.root, sender=sender, now_epoch=2000)

        self.assertEqual(first["delivered"], 1)
        self.assertEqual(second["attempted"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            OwnerNotificationStore(self.root).iter_events()[0]["status"],
            STATUS_DELIVERED,
        )

    def test_failed_delivery_uses_bounded_retry_delay(self):
        self._request()
        calls = []

        def sender(_event):
            calls.append(1)
            return {"ok": False, "error_code": "desktop_session_unavailable"}

        first = pump_owner_notifications(self.root, sender=sender, now_epoch=1000)
        second = pump_owner_notifications(self.root, sender=sender, now_epoch=1010)
        third = pump_owner_notifications(self.root, sender=sender, now_epoch=1030)

        self.assertEqual(first["attempted"], 1)
        self.assertEqual(second["attempted"], 0)
        self.assertEqual(second["deferred_retry"], 1)
        self.assertEqual(third["attempted"], 1)
        self.assertEqual(len(calls), 2)

    def test_resolved_source_cancels_only_undelivered_event(self):
        request = self._request()
        store = OwnerNotificationStore(self.root)
        store.reconcile_access_requests()
        event = store.iter_events()[0]

        self.access.approve(request["request_id"])
        result = store.reconcile_access_requests()

        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(store.load(event["notification_id"])["status"], STATUS_CANCELLED)

    def test_delivered_event_remains_historical_after_access_resolution(self):
        request = self._request()
        pump_owner_notifications(
            self.root,
            sender=lambda _event: {"ok": True},
            now_epoch=1000,
        )
        event = OwnerNotificationStore(self.root).iter_events()[0]

        self.access.approve(request["request_id"])
        OwnerNotificationStore(self.root).reconcile_access_requests()

        self.assertEqual(
            OwnerNotificationStore(self.root).load(event["notification_id"])["status"],
            STATUS_DELIVERED,
        )

    def test_only_delivered_notification_can_be_acknowledged(self):
        self._request()
        store = OwnerNotificationStore(self.root)
        store.reconcile_access_requests()
        event = store.iter_events()[0]

        with self.assertRaisesRegex(ValueError, "only delivered"):
            store.acknowledge(event["notification_id"])

        pump_owner_notifications(
            self.root,
            sender=lambda _event: {"ok": True},
            now_epoch=1000,
        )
        acknowledged = store.acknowledge(event["notification_id"])
        self.assertEqual(acknowledged["status"], STATUS_ACKNOWLEDGED)

    def test_sender_exception_does_not_escape_pump(self):
        self._request()

        def sender(_event):
            raise RuntimeError("boom")

        result = pump_owner_notifications(self.root, sender=sender, now_epoch=1000)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["delivered"], 0)
        event = OwnerNotificationStore(self.root).iter_events()[0]
        self.assertEqual(event["status"], STATUS_PENDING)
        self.assertEqual(event["last_error_code"], "sender_RuntimeError")

    def test_corrupt_and_symlinked_events_are_ignored(self):
        events = self.root / "runtime" / "owner_notifications" / "events"
        events.mkdir(parents=True)
        (events / ("nt_" + "0" * 32 + ".json")).write_text("{", encoding="utf-8")
        target = events / "target.json"
        target.write_text("{}", encoding="utf-8")
        (events / ("nt_" + "1" * 32 + ".json")).symlink_to(target)

        self.assertEqual(OwnerNotificationStore(self.root).iter_events(), ())

    def test_direct_script_cli_lists_json_without_import_error(self):
        import json
        import subprocess

        script = ROOT / "src" / "sira" / "owner_notifications.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--root",
                str(self.root),
                "list",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema"], "sira.owner_notifications.v1")
        self.assertEqual(payload["event_count"], 0)


if __name__ == "__main__":
    unittest.main()
