"""Current-HEAD release evidence stays truthful in the sanitized demo."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sira.competition_demo import Handler, _Server, build_public_snapshot, render_html
from sira.desktop_app import DesktopControl
from sira.self_model import _release_evidence


class CurrentReleaseTests(unittest.TestCase):
    def test_current_head_acceptance_retains_real_dynamic_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / "runtime/release"
            directory.mkdir(parents=True)
            checks = {"engineering_canary_21_21": True,
                      "protected_surface_unchanged": True,
                      "source_surface_unchanged": True,
                      "two_real_soaks_passed": True,
                      "multicycle_real_soak_passed": True}
            checks.update({f"additional_{n}": True for n in range(18)})
            report = {"status": "passed", "release_ready": True,
                      "created_at": datetime.now(timezone.utc).isoformat(),
                      "git": {"revision": "cc7d452", "clean": True},
                      "checks": checks, "required_check_count": 23,
                      "required_checks_passed": 23}
            path = directory / ("release_acceptance_" + "a" * 32 + ".json")
            path.write_text(json.dumps(report), encoding="utf-8")

            old = _release_evidence(root, "94c1307", datetime.now(timezone.utc))
            self.assertFalse(old["release_ready"])
            self.assertNotIn("required_check_count", old)

            current = _release_evidence(root, "cc7d452", datetime.now(timezone.utc))
            self.assertTrue(current["release_ready"])
            self.assertEqual(current["required_check_count"], 23)
            self.assertEqual(current["required_checks_passed"], 23)

    def test_desktop_keeps_required_and_passed_counts_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            control = object.__new__(DesktopControl)
            control.root = Path(folder)
            class Core:
                def snapshot(self):
                    return {}
            control.core = Core()
            state = {"identity": {"head": "cc7d452"}, "runtime": {},
                     "release": {"release_ready": False,
                                 "reason": "release_acceptance_failed",
                                 "required_check_count": 23,
                                 "required_checks_passed": 22}}
            with patch("sira.desktop_app.compact_core_state", return_value=state), \
                 patch("sira.desktop_app._git_summary", return_value={"revision": "cc7d452", "clean": True}), \
                 patch("sira.desktop_app._memory_summary", return_value={}), \
                 patch("sira.desktop_app._notification_summary", return_value={"readable": True, "total": 0, "by_status": {}, "latest": None, "truncated": False}), \
                 patch("sira.desktop_app._activity", return_value=[]):
                view = control.overview()
            self.assertFalse(view["release"]["release_ready"])
            self.assertEqual(view["release"]["checks_required"], 23)
            self.assertEqual(view["release"]["checks_passed"], 22)

    def test_public_view_does_not_override_current_head_failure_with_core_hit(self):
        class Control:
            def __init__(self, root):
                pass
            def overview(self):
                return {"release": {"release_ready": False,
                                    "decision_code": "no_current_release_evidence",
                                    "checks_required": None, "checks_passed": None},
                        "core": {"release": {"release_ready": True,
                                             "required_check_count": 23,
                                             "required_checks_passed": 23},
                                 "runtime": {"effective_state": "running"}}}
        with patch("sira.competition_demo.DesktopControl", Control):
            public = build_public_snapshot(Path("."))
        self.assertFalse(public["release"]["ready"])
        self.assertIsNone(public["release"]["required_checks"])
        self.assertIsNone(public["release"]["passed_checks"])
        self.assertIn("current", render_html(public).lower())

    def test_public_view_reports_real_counts_only_for_current_head_acceptance(self):
        class Control:
            def __init__(self, root):
                pass
            def overview(self):
                return {"release": {"release_ready": True,
                                    "decision_code": "current_head_release_gate_passed",
                                    "checks_required": 23, "checks_passed": 23},
                        "core": {"release": {"release_ready": False},
                                 "runtime": {"effective_state": "running"}}}
        with patch("sira.competition_demo.DesktopControl", Control):
            public = build_public_snapshot(Path("."))
        self.assertTrue(public["release"]["ready"])
        self.assertEqual(public["release"]["required_checks"], 23)
        self.assertEqual(public["release"]["passed_checks"], 23)
        self.assertIn("23/23 required checks", render_html(public))

    def test_loopback_http_surface_is_read_only(self):
        class Control:
            def __init__(self, root):
                pass
            def overview(self):
                return {"release": {"release_ready": False}, "core": {"runtime": {}}}
        with tempfile.TemporaryDirectory() as folder, patch("sira.competition_demo.DesktopControl", Control):
            with _Server(("127.0.0.1", 0), Handler, root=Path(folder)) as server:
                worker = Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    base = f"http://127.0.0.1:{server.server_port}"
                    for path in ("/healthz", "/api/demo/overview", "/"):
                        with urlopen(base + path, timeout=3) as response:
                            self.assertEqual(response.status, 200)
                    for method in ("POST", "PUT", "PATCH", "DELETE"):
                        with self.assertRaises(HTTPError) as raised:
                            urlopen(Request(base + "/api/demo/overview", method=method), timeout=3)
                        self.assertEqual(raised.exception.code, 405)
                        raised.exception.close()
                finally:
                    server.shutdown()
                    worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
