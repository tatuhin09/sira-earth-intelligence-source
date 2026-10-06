"""A84.2: one bounded owner outbox view powers Dashboard and Notifications."""
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.desktop_app import DesktopControl, DesktopRequestHandler, _Server
from sira.owner_notifications import OwnerNotificationStore
from sira.runtime import RuntimeStateStore


class NotificationVisibilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="sira-a842-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        RuntimeStateStore(self.root).save({
            "desired_state": "off", "worker_state": "stopped", "generation": 69,
        })
        self.store = OwnerNotificationStore(self.root)

    def notice(self, **overrides):
        notice = {"request_id": "ar_" + "a" * 32, "resource": "web search",
                  "reason": "The provider credential is unavailable.",
                  "owner_action": "Review local access settings.", "risk": "moderate"}
        notice.update(overrides)
        return self.store.enqueue_access_notice(notice)

    def test_actual_stored_event_is_bounded_and_same_in_overview_and_page(self):
        event = self.notice()
        control = DesktopControl(self.root)
        page = control.notifications()
        dashboard = control.overview()["notifications"]
        self.assertEqual(page["total"], dashboard["total"])
        self.assertEqual(page["total"], len(page["events"]))
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["events"][0]["notification_id"], event["notification_id"])
        self.assertEqual(dashboard["latest"], page["events"][-1])
        self.assertIn("provider credential", page["events"][0]["body"])
        self.assertEqual(page["events"][0]["source_kind"], "access_request")
        self.assertEqual(page["events"][0]["category"], "owner_access_required")
        self.assertEqual(page["events"][0]["source_request_id"], event["source_request_id"])

    def test_secret_bearing_event_is_never_sent_to_desktop(self):
        event = self.notice()
        path = self.store._path(event["notification_id"])
        raw = json.loads(path.read_text())
        raw["body"] = "Use GEMINI_API_KEY=example-private-credential and Bearer private-token-value"
        path.write_text(json.dumps(raw), encoding="utf-8")
        data = DesktopControl(self.root).notifications()
        self.assertNotIn("example-private-credential", json.dumps(data))
        self.assertNotIn("private-token-value", json.dumps(data))
        raw["contains_secret_value"] = True
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(DesktopControl(self.root).notifications()["total"], 0)

    def test_empty_refresh_does_not_create_notification(self):
        control = DesktopControl(self.root)
        for _ in range(2):
            self.assertEqual(control.notifications()["total"], 0)
            self.assertEqual(control.overview()["notifications"]["total"], 0)
        self.assertEqual(self.store.iter_events(), ())

    def test_local_notification_http_route_returns_the_same_safe_snapshot(self):
        self.notice()
        static = Path(__file__).resolve().parents[1] / "desktop/static"
        with _Server(("127.0.0.1", 0), DesktopRequestHandler, root=self.root,
                     static_dir=static, session_token="a842-local-test") as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.shutdown)
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/api/notifications", timeout=5) as response:
                page = json.load(response)
            with urlopen(base + "/api/overview", timeout=5) as response:
                dashboard = json.load(response)
        self.assertEqual(page["events"][-1], dashboard["notifications"]["latest"])
        self.assertEqual(page["total"], dashboard["notifications"]["total"])

    def test_deleted_or_mismatched_outbox_record_does_not_count(self):
        event = self.notice()
        path = self.store._path(event["notification_id"])
        raw = json.loads(path.read_text())
        raw["notification_id"] = "nt_" + "f" * 32
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(DesktopControl(self.root).notifications()["total"], 0)
        path.unlink()
        self.assertEqual(DesktopControl(self.root).overview()["notifications"]["total"], 0)

    def test_invalid_status_fails_closed_and_page_is_bounded(self):
        event = self.notice()
        path = self.store._path(event["notification_id"])
        raw = json.loads(path.read_text())
        raw["status"] = ["pending"]
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(DesktopControl(self.root).notifications()["total"], 0)

    def test_count_is_the_visible_bounded_set(self):
        for index in range(52):
            self.notice(request_id=f"ar_{index:032x}")
        control = DesktopControl(self.root)
        page = control.notifications()
        dashboard = control.overview()["notifications"]
        self.assertEqual(page["total"], 50)
        self.assertEqual(len(page["events"]), 50)
        self.assertEqual(dashboard["total"], page["total"])
        self.assertTrue(page["truncated"])
        self.assertEqual(page["latest"], dashboard["latest"])

    def test_notification_view_and_research_meaning_are_explicit(self):
        static = Path(__file__).resolve().parents[1] / "desktop/static"
        html = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-view-jump="notifications"', html)
        self.assertIn('id="core-research-capability"', html)
        self.assertIn('id="notification-list"', html)
        self.assertIn("Persistent Memory", script)
        self.assertIn("Verified Knowledge", script)
        self.assertIn("No owner notifications recorded", script)
        self.assertIn("Verified research-job evidence exists; broader capability threshold is not satisfied", script)


if __name__ == "__main__":
    unittest.main()
