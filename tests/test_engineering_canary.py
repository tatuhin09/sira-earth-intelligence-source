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
from sira.engineering_canary import (
    EXPECTED_CHECK_COUNT,
    run_engineering_canary_check,
)
from sira.engineering_authorization import _surface_snapshot
from sira.runtime import RuntimeStateStore
from sira.self_modification import PROTECTED_PATHS


class EngineeringCanaryTests(unittest.TestCase):
    def test_canary_passes_offline_and_preserves_real_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before, _ = _surface_snapshot(root)
            report = run_engineering_canary_check(root)
            after, _ = _surface_snapshot(root)
            status = RuntimeStateStore(root).status()

        self.assertEqual(report["status"], "passed")
        self.assertEqual(
            (report["passed"], report["failed"]),
            (EXPECTED_CHECK_COUNT, 0),
        )
        self.assertEqual(before, after)
        self.assertTrue(
            report["soak_readiness"][
                "ready_for_bounded_soak"
            ]
        )
        self.assertFalse(
            report["soak_readiness"][
                "persistent_runtime_started"
            ]
        )
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(
            report["metered_model_requests"],
            0,
        )
        self.assertFalse(report["paid_spending"])
        self.assertEqual(
            status["effective_state"],
            "stopped",
        )

    def test_canary_refuses_when_runtime_is_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            RuntimeStateStore(root).save({
                "desired_state": "on",
                "worker_state": "running",
                "pid": 999999,
                "started_at": "2026-09-21T00:00:00+00:00",
                "heartbeat_at": "2026-09-21T00:00:00+00:00",
                "generation": 1,
            })
            report = run_engineering_canary_check(root)

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            report["decision_code"],
            "runtime_must_be_off",
        )
        self.assertFalse(
            report["soak_readiness"][
                "ready_for_bounded_soak"
            ]
        )
        self.assertIsNone(report["artifact"])

    def test_canary_module_is_protected(self):
        self.assertIn(
            "src/sira/engineering_canary.py",
            PROTECTED_PATHS,
        )

    def test_cli_self_canary_check_prints_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = main([
                    "--root",
                    tmp,
                    "self",
                    "canary-check",
                ])
            payload = json.loads(out.getvalue())

        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertTrue(
            payload["soak_readiness"][
                "ready_for_bounded_soak"
            ]
        )


if __name__ == "__main__":
    unittest.main()
