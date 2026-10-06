from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.cli import main
from sira.runtime import RuntimeStateStore
from sira.runtime_release import run_release_acceptance
from sira.self_modification import PROTECTED_PATHS
from sira.storage import write_json


class RuntimeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        RuntimeStateStore(self.root).save({
            "desired_state": "off",
            "worker_state": "stopped",
            "pid": None,
            "started_at": "2026-09-21T10:00:00+00:00",
            "heartbeat_at": "2026-09-21T10:00:00+00:00",
            "generation": 2,
        })
        self._write_soak(1, 1)
        self._write_soak(2, 2)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_soak(self, cycles, generation):
        write_json(
            self.root
            / "runtime"
            / "soak"
            / f"bounded_soak_{generation:032x}.json",
            {
                "schema": "sira.runtime_soak_report.v1",
                "status": "passed",
                "decision_code": "bounded_real_soak_passed",
                "created_at": f"2026-09-21T10:{generation:02d}:00+00:00",
                "generation": generation,
                "cycles_completed": cycles,
                "checks": {
                    "completed_at_least_one_cycle": True,
                    "loop_stopped_cleanly": True,
                    "runtime_off_after": True,
                    "protected_surface_unchanged": True,
                    "no_failed_cycle": True,
                    "no_unauthorized_source_mutation": True,
                },
                "coverage": {"promotion_observed": False},
            },
        )

    @staticmethod
    def diag(_root):
        return {
            "status": "passed",
            "passed": 21,
            "failed": 0,
            "api_requests": 0,
            "metered_model_requests": 0,
            "artifact": None,
        }

    @staticmethod
    def workers(_root):
        return {
            "status": "passed",
            "api_requests": 0,
            "artifact": None,
        }

    @staticmethod
    def memory(_root):
        return {
            "primary": {"healthy": True},
            "backup": {"healthy": True},
            "parity": True,
        }

    def _run(self, **kwargs):
        return run_release_acceptance(
            self.root,
            canary_runner=self.diag,
            reliability_runner=self.diag,
            workers_runner=self.workers,
            git_probe=lambda _root: {
                "available": True,
                "clean": True,
                "revision": "fixture",
            },
            memory_inspector=self.memory,
            **kwargs,
        )

    def test_release_acceptance_passes_with_two_real_soaks(self):
        report = self._run()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["decision_code"], "release_ready")
        self.assertTrue(report["release_ready"])
        self.assertTrue(all(report["checks"].values()))
        self.assertTrue(Path(report["artifact"]).is_file())

    def test_release_requires_multicycle_soak(self):
        soak_dir = self.root / "runtime" / "soak"
        for path in soak_dir.glob("*.json"):
            path.unlink()
        self._write_soak(1, 1)
        self._write_soak(1, 2)
        report = self._run()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(
            report["checks"]["multicycle_real_soak_passed"]
        )

    def test_release_requires_memory_parity(self):
        report = run_release_acceptance(
            self.root,
            canary_runner=self.diag,
            reliability_runner=self.diag,
            workers_runner=self.workers,
            git_probe=lambda _root: {
                "available": True,
                "clean": True,
                "revision": "fixture",
            },
            memory_inspector=lambda _root: {
                "primary": {"healthy": True},
                "backup": {"healthy": True},
                "parity": False,
            },
        )
        self.assertEqual(report["status"], "failed")
        self.assertFalse(
            report["checks"]["memory_primary_backup_parity"]
        )

    def test_release_is_blocked_while_runtime_is_on(self):
        RuntimeStateStore(self.root).save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": 999999,
            "started_at": "2026-09-21T10:00:00+00:00",
            "heartbeat_at": "2026-09-21T10:00:00+00:00",
            "generation": 3,
        })
        report = self._run()
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            report["decision_code"],
            "runtime_must_be_off",
        )

    def test_preflight_can_ignore_only_git_cleanliness(self):
        report = run_release_acceptance(
            self.root,
            require_git_clean=False,
            canary_runner=self.diag,
            reliability_runner=self.diag,
            workers_runner=self.workers,
            git_probe=lambda _root: {
                "available": True,
                "clean": False,
                "revision": "fixture",
            },
            memory_inspector=self.memory,
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(
            report["decision_code"],
            "release_preflight_passed",
        )
        self.assertFalse(report["release_ready"])
        self.assertTrue(report["preflight_ready"])

    def test_release_module_is_protected(self):
        self.assertIn(
            "src/sira/runtime_release.py",
            PROTECTED_PATHS,
        )

    def test_cli_release_check_prints_json(self):
        fake = {
            "status": "passed",
            "decision_code": "release_ready",
            "release_ready": True,
        }
        out = io.StringIO()
        with patch(
            "sira.cli.run_release_acceptance",
            return_value=fake,
        ) as runner:
            with redirect_stdout(out):
                rc = main([
                    "--root",
                    str(self.root),
                    "self",
                    "release-check",
                ])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()), fake)
        runner.assert_called_once_with(self.root.resolve())


if __name__ == "__main__":
    unittest.main()
