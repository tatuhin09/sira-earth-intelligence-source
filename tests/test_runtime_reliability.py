import json
from pathlib import Path
import tempfile
import unittest
import sys
import io
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class RuntimeReliabilityStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_metered_budget_enforces_min_interval_and_rolling_limit(self):
        from sira.runtime_reliability import RuntimeReliabilityStore

        store = RuntimeReliabilityStore(self.root)
        first = store.metered_budget(now_epoch=1000.0)
        self.assertTrue(first["allowed"])
        store.record_metered_attempt(now_epoch=1000.0)

        too_soon = store.metered_budget(now_epoch=1050.0)
        self.assertFalse(too_soon["allowed"])
        self.assertEqual(too_soon["reason"], "minimum_interval")
        self.assertGreater(too_soon["retry_after_seconds"], 0)

        store.record_metered_attempt(now_epoch=1200.0)
        store.record_metered_attempt(now_epoch=1400.0)
        exhausted = store.metered_budget(now_epoch=1500.0)
        self.assertFalse(exhausted["allowed"])
        self.assertEqual(exhausted["reason"], "rolling_budget_exhausted")

        reopened = store.metered_budget(now_epoch=2001.0)
        self.assertTrue(reopened["allowed"])

    def test_transient_failures_back_off_exponentially_and_success_resets_streak(self):
        from sira.runtime_reliability import RuntimeReliabilityStore

        store = RuntimeReliabilityStore(self.root)
        first = store.record_cycle({"status": "completed", "outcome": "http_503"}, now_epoch=1000.0)
        second = store.record_cycle({"status": "completed", "outcome": "http_503"}, now_epoch=1030.0)
        self.assertEqual(first["failure_class"], "transient_external")
        self.assertGreater(second["delay_seconds"], first["delay_seconds"])

        success = store.record_cycle({"status": "completed", "outcome": "promotion_committed"}, now_epoch=1200.0)
        self.assertEqual(success["failure_class"], "none")
        self.assertEqual(success["transient_streak"], 0)

    def test_configuration_failure_uses_long_cooldown_but_is_not_terminal(self):
        from sira.runtime_reliability import RuntimeReliabilityStore

        store = RuntimeReliabilityStore(self.root)
        decision = store.record_cycle(
            {"status": "completed", "outcome": "missing_model_key", "error": {"type": "ValueError"}},
            now_epoch=1000.0,
        )
        self.assertEqual(decision["failure_class"], "configuration")
        self.assertGreaterEqual(decision["delay_seconds"], 900)
        self.assertTrue(decision["worker_should_continue"])


class InterruptedCycleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_interrupted_cycle_is_archived_and_not_resumed_mid_transaction(self):
        from sira.runtime_reliability import ActiveCycleJournal, recover_interrupted_cycle

        journal = ActiveCycleJournal(self.root)
        active = journal.start(generation=4, cycle_id="sc_" + "a" * 32, phase="research")
        journal.update("writer_handoff")
        report = recover_interrupted_cycle(self.root, recovered_by_generation=5)

        self.assertEqual(report["status"], "recovered_interrupted_cycle")
        self.assertEqual(report["action"], "abandoned_and_reselect")
        self.assertEqual(report["interrupted_cycle_id"], active["cycle_id"])
        self.assertEqual(report["interrupted_phase"], "writer_handoff")
        self.assertFalse(journal.path.exists())
        archived = Path(report["artifact"])
        self.assertTrue(archived.is_file())
        payload = json.loads(archived.read_text(encoding="utf-8"))
        self.assertEqual(payload["action"], "abandoned_and_reselect")

    def test_cleanly_finished_cycle_leaves_nothing_to_recover(self):
        from sira.runtime_reliability import ActiveCycleJournal, recover_interrupted_cycle

        journal = ActiveCycleJournal(self.root)
        journal.start(generation=4, cycle_id="sc_" + "b" * 32, phase="selection")
        journal.finish(status="completed", outcome="promotion_committed")
        report = recover_interrupted_cycle(self.root, recovered_by_generation=5)
        self.assertEqual(report["status"], "nothing_to_recover")


class ReliabilityCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


    def test_cli_exposes_reliability_check(self):
        from sira.cli import main

        fake = {
            "status": "passed",
            "checks": {"recovery": True},
            "api_requests": 0,
            "metered_model_requests": 0,
            "paid_spending": False,
        }
        out = io.StringIO()
        with patch("sira.cli.run_reliability_check", return_value=fake):
            with redirect_stdout(out):
                rc = main(["--root", str(self.root), "self", "reliability-check"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "passed")

    def test_reliability_check_is_local_only(self):
        from sira.runtime_reliability import run_reliability_check

        report = run_reliability_check(self.root)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(report["metered_model_requests"], 0)
        self.assertFalse(report["paid_spending"])
        self.assertTrue(all(report["checks"].values()))


if __name__ == "__main__":
    unittest.main()
