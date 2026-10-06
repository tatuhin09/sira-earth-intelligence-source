"""Public export is a distinct trust boundary from the owner's live demo."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from sira.competition_export import (
    PUBLIC_FILES, PublicExportError, audit_public_files, export_public_site,
    project_public_snapshot,
)


def source():
    return {
        "schema": "sira.competition_demo.v1",
        "generated_at": "2026-09-30T08:00:00+00:00",
        "identity": {"name": "SIRA", "description": "private-name@example.com"},
        "runtime": {"state": "running", "health": "ok", "generation": 74,
                    "worker_state": "running", "recent_outcome": "lg_" + "a" * 32},
        "release": {"ready": True, "required_checks": 19, "passed_checks": 19},
        "knowledge": {"verified_count": 4, "conflicted_count": 0,
                      "private_content_exposed": False, "claim": "private memory text"},
        "learning": {"active_goal_count": 5, "goals": [{"topic": "Personal quantum idea 7788"}]},
        "providers": {"known_count": 11, "available_count": 9,
                      "cooling_count": 0, "credentials": "sk-secret-key"},
        "capabilities": [
            {"name": "research", "state": "partially_demonstrated",
             "scope": "private-name@example.com", "evidence_refs": ["private-ID"]},
            {"name": "skill_practice", "state": "unverified", "scope": "private scope"},
            {"name": "unknown_sensitive", "state": "demonstrated", "scope": "secret"},
        ],
        "research": {"capability_state": "partially_demonstrated"},
        "last_task": {"status": "completed", "route": "self_status",
                      "verification_status": "not_applicable_operational_status",
                      "task_id": "private-task"},
        "authority": {"runtime_control_exposed": False,
                      "public_mutation_endpoints": False,
                      "promotion_authorized": False,
                      "paid_spending_authorized": False,
                      "skill_activated": False},
        "privacy": {"api_keys_exposed": False, "filesystem_paths_exposed": False,
                    "source_code_browser_exposed": False,
                    "terminal_or_tool_execution_exposed": False},
    }


class PublicExportTests(unittest.TestCase):
    def test_projection_removes_private_values_and_preserves_scoped_evidence(self):
        exported = project_public_snapshot(source())
        encoded = json.dumps(exported)
        for private in ("private-name@example.com", "private memory text", "Personal quantum idea 7788",
                        "sk-secret-key", "private-ID", "private-task", "private scope",
                        "unknown_sensitive", "lg_" + "a" * 32):
            self.assertNotIn(private, encoded)
        self.assertEqual(exported["release"]["required_checks"], 19)
        self.assertEqual(exported["runtime"]["generation"], 74)
        self.assertEqual(exported["capabilities"][0]["state"], "partially_demonstrated")
        self.assertEqual(exported["capabilities"][1]["state"], "unverified")
        self.assertIn("captured_at", exported)
        self.assertNotIn("live", encoded.lower())

    def test_release_must_be_valid_current_evidence(self):
        for release in ({"ready": False, "required_checks": None, "passed_checks": None},
                        {"ready": True, "required_checks": 19, "passed_checks": 18},
                        {"ready": True, "required_checks": True, "passed_checks": True}):
            value = source()
            value["release"] = release
            with self.subTest(release=release), self.assertRaises(PublicExportError):
                project_public_snapshot(value)

    def test_public_authority_must_be_disabled(self):
        value = source()
        value["authority"]["promotion_authorized"] = True
        with self.assertRaises(PublicExportError):
            project_public_snapshot(value)

    def test_value_audit_blocks_secret_even_in_static_template(self):
        files = {name: b"safe" for name in PUBLIC_FILES}
        files["index.html"] = b"<p>private-session-7788</p>"
        with self.assertRaises(PublicExportError):
            audit_public_files(files, ("private-session-7788",))

    def test_export_rejects_stale_revision_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "public"
            control = {"release": {"revision": "oldhead", "release_ready": True,
                                    "checks_required": 19, "checks_passed": 19}}
            with patch("sira.competition_export.current_git_state", return_value=("newhead", True)), \
                 patch("sira.competition_export.DesktopControl") as desktop, \
                 patch("sira.competition_export.build_public_snapshot", return_value=source()):
                desktop.return_value.overview.return_value = control
                with self.assertRaises(PublicExportError):
                    export_public_site(root, output)
            self.assertFalse(output.exists())

    def test_zip_contains_only_static_allowlisted_site_and_docs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "public"
            archive = root / "site.zip"
            control = {"release": {"revision": "2e264f3", "release_ready": True,
                                    "checks_required": 19, "checks_passed": 19}}
            with patch("sira.competition_export.current_git_state", return_value=("2e264f3", True)), \
                 patch("sira.competition_export.DesktopControl") as desktop, \
                 patch("sira.competition_export.build_public_snapshot", return_value=source()):
                desktop.return_value.overview.return_value = control
                export_public_site(root, output, archive=archive,
                                   private_values=("private-name@example.com", "private memory text"))
            self.assertEqual({str(p.relative_to(output)) for p in output.rglob('*') if p.is_file()},
                             set(PUBLIC_FILES))
            with ZipFile(archive) as package:
                self.assertEqual(set(package.namelist()), set(PUBLIC_FILES))
                html = package.read("index.html").decode()
                docs = package.read("docs/index.html").decode()
                data = json.loads(package.read("snapshot.json"))
            self.assertIn("19/19", html)
            self.assertIn("href=\"/docs/\"", html)
            self.assertIn("Research", docs)
            self.assertEqual(data["release"]["passed_checks"], 19)
            for value in ("private-name@example.com", "private memory text", "127.0.0.1:8765",
                          "127.0.0.1:8877", "Personal quantum idea 7788"):
                self.assertNotIn(value, html + docs + json.dumps(data))


if __name__ == "__main__":
    unittest.main()
