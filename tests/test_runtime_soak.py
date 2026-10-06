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
from sira.runtime_soak import (
    run_bounded_real_soak,
)
from sira.self_modification import PROTECTED_PATHS


class RuntimeSoakTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        source = self.root / "src/app.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("VALUE = 1\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def cycle(
        root,
        generation,
        *,
        keep_runtime_on=True,
    ):
        return {
            "status": "completed",
            "outcome": "validated_existing_mitigation",
            "promotion_performed": False,
            "main_tree_modified": False,
            "resource_usage": {
                "api_requests": 0,
                "metered_model_requests": 0,
                "paid_requests": 0,
            },
        }

    def test_two_cycle_soak_auto_stops_and_persists_report(self):
        report = run_bounded_real_soak(
            self.root,
            max_cycles=2,
            max_seconds=60,
            cycle_runner=self.cycle,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(
            report["decision_code"],
            "bounded_real_soak_passed",
        )
        self.assertEqual(report["cycles_completed"], 2)
        self.assertTrue(
            report["checks"]["runtime_off_after"]
        )
        self.assertTrue(
            report["checks"]["protected_surface_unchanged"]
        )
        self.assertTrue(Path(report["artifact"]).is_file())
        status = RuntimeStateStore(self.root).status()
        self.assertEqual(status["desired_state"], "off")
        self.assertEqual(status["effective_state"], "stopped")

    def test_runtime_must_be_off_before_soak(self):
        RuntimeStateStore(self.root).save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": 999999,
            "started_at": "2026-09-21T00:00:00+00:00",
            "heartbeat_at": "2026-09-21T00:00:00+00:00",
            "generation": 1,
        })
        report = run_bounded_real_soak(
            self.root,
            max_cycles=1,
            max_seconds=60,
            cycle_runner=self.cycle,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            report["decision_code"],
            "runtime_must_be_off",
        )

    def test_internal_cycle_failure_fails_soak_but_runtime_stops(self):
        def failed(
            root,
            generation,
            *,
            keep_runtime_on=True,
        ):
            return {
                "status": "failed",
                "outcome": "cycle_error",
                "promotion_performed": False,
                "main_tree_modified": False,
            }

        report = run_bounded_real_soak(
            self.root,
            max_cycles=1,
            max_seconds=60,
            cycle_runner=failed,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(report["status"], "failed")
        self.assertFalse(
            report["checks"]["no_failed_cycle"]
        )
        self.assertEqual(
            RuntimeStateStore(self.root).status()[
                "effective_state"
            ],
            "stopped",
        )

    def test_source_mutation_without_promotion_is_rejected(self):
        def unsafe(
            root,
            generation,
            *,
            keep_runtime_on=True,
        ):
            (Path(root) / "src/app.py").write_text(
                "VALUE = 9\n",
                encoding="utf-8",
            )
            return {
                "status": "completed",
                "outcome": "fixture",
                "promotion_performed": False,
                "main_tree_modified": False,
            }

        report = run_bounded_real_soak(
            self.root,
            max_cycles=1,
            max_seconds=60,
            cycle_runner=unsafe,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(report["status"], "failed")
        self.assertFalse(
            report["checks"][
                "no_unauthorized_source_mutation"
            ]
        )

    def test_promoted_source_change_is_audited_not_rejected(self):
        def promoted(
            root,
            generation,
            *,
            keep_runtime_on=True,
        ):
            (Path(root) / "src/app.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
            return {
                "status": "completed",
                "outcome": "promotion_committed",
                "promotion_performed": True,
                "main_tree_modified": True,
            }

        report = run_bounded_real_soak(
            self.root,
            max_cycles=1,
            max_seconds=60,
            cycle_runner=promoted,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(report["status"], "passed")
        self.assertTrue(
            report["coverage"]["promotion_observed"]
        )
        self.assertTrue(
            report["coverage"]["main_tree_change_observed"]
        )

    def test_cycle_limits_are_strict(self):
        for value in (0, 4, True):
            with self.assertRaises(ValueError):
                run_bounded_real_soak(
                    self.root,
                    max_cycles=value,
                    max_seconds=60,
                    cycle_runner=self.cycle,
                    sleep_fn=lambda _seconds: None,
                )

    def test_soak_module_is_protected(self):
        self.assertIn(
            "src/sira/runtime_soak.py",
            PROTECTED_PATHS,
        )

    def test_cli_soak_run_dispatches_and_returns_json(self):
        fake = {
            "status": "passed",
            "decision_code": "bounded_real_soak_passed",
        }
        out = io.StringIO()
        with patch(
            "sira.cli.run_bounded_real_soak",
            return_value=fake,
        ) as runner:
            with redirect_stdout(out):
                rc = main([
                    "--root",
                    str(self.root),
                    "self",
                    "soak-run",
                    "--cycles",
                    "2",
                    "--max-seconds",
                    "120",
                ])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()), fake)
        runner.assert_called_once_with(
            self.root.resolve(),
            max_cycles=2,
            max_seconds=120,
        )


if __name__ == "__main__":
    unittest.main()
