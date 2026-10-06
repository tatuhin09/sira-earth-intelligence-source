"""Engineering failure circuit breaker: owner-enabled, derived from saved handoffs."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_failure_breaker import (
    BASE_HOLD_SECONDS, breaker_state, filter_opportunities, load_policy,
    write_policy,
)

NOW = 2_000_000_000.0
REJECT, STRUCT, PROMOTE = "candidate_rejected", "structural_goal_not_met", "promotion_committed"


class BreakerBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "improvements/opportunities/handoffs"
        self.directory.mkdir(parents=True)
        self.count = 0
        write_policy(self.root, enabled=True, failure_threshold=3, max_hold_hours=24)

    def handoff(self, kind, outcome, at, *, promoted=False):
        self.count += 1
        data = {"outcome": outcome, "promotion_performed": promoted,
                "cooldown": {"type": kind, "path": f"src/sira/m{self.count}.py", "symbol": "f",
                             "outcome": outcome, "attempted_at_epoch": at}}
        (self.directory / f"ohf_{self.count:032x}.json").write_text(json.dumps(data))

    def failures(self, kind, count, end=NOW - 60, outcome=REJECT):
        for index in range(count):
            self.handoff(kind, outcome, end - (count - 1 - index) * 120)


class BreakerTests(BreakerBase):
    def test_disabled_by_default_and_malformed_policy_fail_safe(self):
        (self.root / "memory/engineering_breaker/policy.json").unlink()
        self.failures("complex_function", 11)
        self.assertEqual(breaker_state(self.root, now_epoch=NOW), {})
        policy = self.root / "memory/engineering_breaker/policy.json"
        policy.write_text("{bad")
        self.assertFalse(load_policy(self.root)["valid"])
        self.assertEqual(breaker_state(self.root, now_epoch=NOW), {})
        for kwargs in ({"failure_threshold": 1}, {"failure_threshold": 31}, {"max_hold_hours": 0},
                       {"max_hold_hours": 999}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                write_policy(self.root, enabled=True, **kwargs)

    def test_trailing_failure_streak_opens_the_breaker_with_a_growing_hold(self):
        self.failures("complex_function", 2)
        self.assertEqual(breaker_state(self.root, now_epoch=NOW), {})
        self.handoff("complex_function", STRUCT, NOW - 30)
        state = breaker_state(self.root, now_epoch=NOW)["complex_function"]
        self.assertEqual((state["streak"], state["hold_seconds"]), (3, BASE_HOLD_SECONDS))
        self.handoff("complex_function", REJECT, NOW - 10)
        state = breaker_state(self.root, now_epoch=NOW)["complex_function"]
        self.assertEqual((state["streak"], state["hold_seconds"]), (4, 2 * BASE_HOLD_SECONDS))

    def test_hold_is_capped_and_expires(self):
        self.failures("complex_function", 11)
        state = breaker_state(self.root, now_epoch=NOW)["complex_function"]
        self.assertEqual(state["hold_seconds"], 24 * 3600)
        later = NOW - 60 + 24 * 3600 + 1
        self.assertEqual(breaker_state(self.root, now_epoch=later), {})

    def test_a_promotion_resets_the_streak(self):
        self.failures("complex_function", 5, end=NOW - 600)
        self.handoff("complex_function", PROMOTE, NOW - 300, promoted=True)
        self.failures("complex_function", 2, end=NOW - 60)
        self.assertEqual(breaker_state(self.root, now_epoch=NOW), {})

    def test_infrastructure_outcomes_neither_count_nor_reset(self):
        self.failures("complex_function", 2, end=NOW - 900)
        self.handoff("complex_function", "writer_validation_error", NOW - 600)
        self.handoff("complex_function", REJECT, NOW - 300)
        self.assertIn("complex_function", breaker_state(self.root, now_epoch=NOW))

    def test_types_are_independent_and_old_attempts_are_ignored(self):
        self.failures("complex_function", 4)
        self.failures("long_function", 1)
        self.failures("dead_code", 4, end=NOW - 8 * 86_400)
        state = breaker_state(self.root, now_epoch=NOW)
        self.assertEqual(set(state), {"complex_function"})

    def test_unreadable_or_oversized_artifacts_are_ignored(self):
        (self.directory / "ohf_bad.json").write_text("{not json")
        (self.directory / "ohf_big.json").write_text(" " * (300 * 1024))
        self.failures("complex_function", 3)
        self.assertIn("complex_function", breaker_state(self.root, now_epoch=NOW))

    def test_filter_removes_only_open_types_and_reports_it(self):
        self.failures("complex_function", 3)
        report = {"opportunities": [{"type": "complex_function", "path": "a"},
                                    {"type": "long_function", "path": "b"}], "eligible_count": 2}
        result = filter_opportunities(self.root, report, now_epoch=NOW)
        self.assertEqual([r["path"] for r in result["opportunities"]], ["b"])
        self.assertEqual(result["failure_breaker"]["suppressed_count"], 1)
        self.assertIn("complex_function", result["failure_breaker"]["open_types"])
        self.assertEqual(len(report["opportunities"]), 2)  # input untouched

    def test_filter_is_a_no_op_when_disabled_or_nothing_is_open(self):
        report = {"opportunities": [{"type": "complex_function", "path": "a"}]}
        self.assertEqual(filter_opportunities(self.root, report, now_epoch=NOW)["opportunities"],
                         report["opportunities"])
        (self.root / "memory/engineering_breaker/policy.json").unlink()
        self.failures("complex_function", 9)
        result = filter_opportunities(self.root, report, now_epoch=NOW)
        self.assertEqual(result["opportunities"], report["opportunities"])
        self.assertEqual(result["failure_breaker"]["open_types"], {})

    def test_default_discovery_path_hides_an_open_type(self):
        from sira import autonomous_targeting
        report = {"opportunities": [{"type": "complex_function", "path": "a"},
                                    {"type": "long_function", "path": "b"}],
                  "eligible_count": 2, "suppressed_cooldown_count": 0}
        with patch.object(autonomous_targeting, "discover_opportunities", return_value=dict(report)):
            before = autonomous_targeting._opportunity_discovery(self.root, 5, NOW)
            self.failures("complex_function", 3)
            after = autonomous_targeting._opportunity_discovery(self.root, 5, NOW)
        self.assertEqual(len(before["opportunities"]), 2)
        self.assertEqual([r["path"] for r in after["opportunities"]], ["b"])
        self.assertEqual(after["failure_breaker"]["suppressed_count"], 1)

    def test_breaker_is_read_only_and_never_grants_authority(self):
        self.failures("complex_function", 4)
        before = sorted(p.name for p in self.directory.iterdir())
        report = filter_opportunities(self.root, {"opportunities": []}, now_epoch=NOW)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), before)
        self.assertNotIn("promotion_authorized", json.dumps(report))
        source = (ROOT / "src/sira/engineering_failure_breaker.py").read_text()
        for forbidden in ("subprocess", "socket", "urllib", "requests", "os.system", "exec(", "eval("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
