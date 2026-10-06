from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sira.provider_health import (
    HEALTH_COOLDOWN,
    HEALTH_DEGRADED_HISTORY,
    HEALTH_METERED_BUDGET_BLOCKED,
    HEALTH_POLICY_BLOCKED,
    HEALTH_READY,
    active_provider_cooldowns,
    build_provider_health_snapshot,
)


def _runtime_status(*, metered_allowed: bool = True) -> dict:
    return {
        "policy_version": "test",
        "transient_streak": 0,
        "next_cycle_in_seconds": 0,
        "last_failure_class": "none",
        "last_outcome": None,
        "metered_budget": {
            "allowed": metered_allowed,
            "reason": "available" if metered_allowed else "minimum_interval",
            "retry_after_seconds": 0 if metered_allowed else 50,
            "attempts_in_window": 0,
            "attempt_limit": 4,
            "window_seconds": 3600,
            "minimum_interval_seconds": 60,
        },
        "state_file": "/tmp/ignored",
    }


def _history(provider: str = "arxiv") -> dict:
    return {
        "provider_reliability": {
            "paper_search": {
                "capability": "paper_search",
                "recurring_unreliable_providers": [
                    {
                        "provider": provider,
                        "failures": 7,
                        "successes": 1,
                        "samples": 8,
                        "failure_rate": 0.875,
                    }
                ],
            }
        },
        "metrics": {},
        "api_requests": 0,
        "paid_spending": False,
    }


class ProviderHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _provider(self, snapshot: dict, provider_id: str) -> dict:
        for row in snapshot["providers"]:
            if row["provider_id"] == provider_id:
                return row
        self.fail(f"provider not found: {provider_id}")

    def test_free_ready_provider_is_available(self):
        snapshot = build_provider_health_snapshot(
            self.root,
            now_epoch=1000.0,
            environ={},
            history={},
            runtime_status=_runtime_status(),
        )
        arxiv = self._provider(snapshot, "arxiv")

        self.assertEqual(arxiv["health"], HEALTH_READY)
        self.assertTrue(arxiv["available_now"])
        self.assertEqual(snapshot["api_requests"], 0)
        self.assertFalse(snapshot["paid_spending"])

    def test_metered_provider_is_policy_blocked_by_default(self):
        snapshot = build_provider_health_snapshot(
            self.root,
            now_epoch=1000.0,
            environ={"TAVILY_API_KEY": "secret"},
            history={},
            runtime_status=_runtime_status(),
        )
        tavily = self._provider(snapshot, "tavily")

        self.assertEqual(tavily["health"], HEALTH_POLICY_BLOCKED)
        self.assertFalse(tavily["available_now"])

    def test_metered_runtime_budget_can_block_explicitly_enabled_provider(self):
        snapshot = build_provider_health_snapshot(
            self.root,
            now_epoch=1000.0,
            allow_metered=True,
            environ={"TAVILY_API_KEY": "secret"},
            history={},
            runtime_status=_runtime_status(metered_allowed=False),
        )
        tavily = self._provider(snapshot, "tavily")

        self.assertEqual(tavily["health"], HEALTH_METERED_BUDGET_BLOCKED)
        self.assertFalse(tavily["available_now"])
        self.assertEqual(tavily["metered_budget_reason"], "minimum_interval")

    def test_active_provider_cooldown_blocks_only_matching_provider(self):
        cooldown_dir = self.root / ".cache" / "opportunity_provider_cooldowns"
        cooldown_dir.mkdir(parents=True)
        (cooldown_dir / "one.json").write_text(
            json.dumps(
                {
                    "provider": "arxiv",
                    "code": "http_429",
                    "recorded_at_epoch": 990.0,
                    "until_epoch": 1100.0,
                }
            ),
            encoding="utf-8",
        )

        snapshot = build_provider_health_snapshot(
            self.root,
            now_epoch=1000.0,
            environ={},
            history={},
            runtime_status=_runtime_status(),
        )
        arxiv = self._provider(snapshot, "arxiv")
        crossref = self._provider(snapshot, "crossref")

        self.assertEqual(arxiv["health"], HEALTH_COOLDOWN)
        self.assertFalse(arxiv["available_now"])
        self.assertEqual(arxiv["cooldown_code"], "http_429")
        self.assertEqual(arxiv["cooldown_remaining_seconds"], 100.0)
        self.assertEqual(crossref["health"], HEALTH_READY)

    def test_expired_malformed_symlink_and_unknown_cooldowns_are_ignored(self):
        cooldown_dir = self.root / ".cache" / "opportunity_provider_cooldowns"
        cooldown_dir.mkdir(parents=True)
        (cooldown_dir / "expired.json").write_text(
            json.dumps(
                {
                    "provider": "arxiv",
                    "code": "http_429",
                    "until_epoch": 999.0,
                }
            ),
            encoding="utf-8",
        )
        (cooldown_dir / "bad.json").write_text("{", encoding="utf-8")
        (cooldown_dir / "unknown.json").write_text(
            json.dumps(
                {
                    "provider": "not-a-catalog-provider",
                    "code": "timeout",
                    "until_epoch": 1200.0,
                }
            ),
            encoding="utf-8",
        )
        target = cooldown_dir / "target.json"
        target.write_text(
            json.dumps(
                {
                    "provider": "crossref",
                    "code": "timeout",
                    "until_epoch": 1200.0,
                }
            ),
            encoding="utf-8",
        )
        symlink = cooldown_dir / "link.json"
        symlink.symlink_to(target)

        cooldowns = active_provider_cooldowns(
            self.root,
            now_epoch=1000.0,
        )

        self.assertEqual(cooldowns, {"crossref": cooldowns["crossref"]})
        self.assertEqual(cooldowns["crossref"]["code"], "timeout")

    def test_recurring_unreliable_history_marks_degraded_but_not_blocked(self):
        snapshot = build_provider_health_snapshot(
            self.root,
            now_epoch=1000.0,
            environ={},
            history=_history("ArXiv"),
            runtime_status=_runtime_status(),
        )
        arxiv = self._provider(snapshot, "arxiv")

        self.assertEqual(arxiv["health"], HEALTH_DEGRADED_HISTORY)
        self.assertTrue(arxiv["available_now"])
        self.assertTrue(arxiv["historical_unreliable"])
        self.assertEqual(arxiv["historical_failures"], 7)
        self.assertEqual(arxiv["historical_capabilities"], ["paper_search"])

    def test_cooldown_has_precedence_over_degraded_history(self):
        cooldown_dir = self.root / ".cache" / "opportunity_provider_cooldowns"
        cooldown_dir.mkdir(parents=True)
        (cooldown_dir / "one.json").write_text(
            json.dumps(
                {
                    "provider": "arxiv",
                    "code": "temporarily_unavailable",
                    "until_epoch": 1200.0,
                }
            ),
            encoding="utf-8",
        )
        snapshot = build_provider_health_snapshot(
            self.root,
            now_epoch=1000.0,
            environ={},
            history=_history("arxiv"),
            runtime_status=_runtime_status(),
        )
        arxiv = self._provider(snapshot, "arxiv")

        self.assertEqual(arxiv["health"], HEALTH_COOLDOWN)
        self.assertTrue(arxiv["historical_unreliable"])

    def test_secret_values_never_appear_in_snapshot(self):
        secret = "do-not-leak-this-value"
        snapshot = build_provider_health_snapshot(
            self.root,
            now_epoch=1000.0,
            allow_metered=True,
            environ={
                "TAVILY_API_KEY": secret,
                "SIRA_GITHUB_TOKEN": secret,
                "SIRA_SEMANTIC_SCHOLAR_API_KEY": secret,
            },
            history={},
            runtime_status=_runtime_status(),
        )

        self.assertNotIn(secret, json.dumps(snapshot, sort_keys=True))

    def test_default_snapshot_reads_only_local_state_on_empty_root(self):
        snapshot = build_provider_health_snapshot(
            self.root,
            now_epoch=1000.0,
            environ={},
        )

        self.assertEqual(snapshot["schema"], "sira.provider_health.v1")
        self.assertEqual(snapshot["provider_count"], 11)
        self.assertEqual(snapshot["api_requests"], 0)
        self.assertFalse(snapshot["paid_spending"])
        self.assertIn("global_runtime", snapshot)


if __name__ == "__main__":
    unittest.main()
