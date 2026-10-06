from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sira.provider_router_advisory import (
    ADVISORY_SCHEMA,
    build_router_advisory,
    main,
)


def _snapshot(rows: list[dict]) -> dict:
    return {
        "schema": "sira.provider_health.v1",
        "providers": rows,
        "api_requests": 0,
        "paid_spending": False,
    }


def _row(
    provider_id: str,
    *,
    health: str = "ready",
    available_now: bool = True,
    cost_class: str = "free",
    historical_unreliable: bool = False,
    cooldown_active: bool = False,
    policy_reason: str = "ready",
) -> dict:
    return {
        "provider_id": provider_id,
        "health": health,
        "available_now": available_now,
        "cost_class": cost_class,
        "historical_unreliable": historical_unreliable,
        "cooldown_active": cooldown_active,
        "policy_reason": policy_reason,
    }


class ProviderRouterAdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ready_free_paper_providers_are_recommended_deterministically(self):
        snapshot = _snapshot([
            _row("semantic_scholar"),
            _row("openalex"),
            _row("crossref"),
            _row("arxiv"),
        ])
        advisory = build_router_advisory(
            self.root,
            capability="paper_search",
            health_snapshot=snapshot,
        )
        self.assertEqual(
            advisory["recommended_provider_ids"],
            ["arxiv", "crossref", "openalex", "semantic_scholar"],
        )
        self.assertEqual(advisory["blocked_count"], 0)

    def test_degraded_provider_is_lower_priority_not_blocked(self):
        snapshot = _snapshot([
            _row("arxiv", health="degraded_history", historical_unreliable=True),
            _row("crossref"),
            _row("openalex"),
            _row("semantic_scholar"),
        ])
        advisory = build_router_advisory(
            self.root,
            capability="paper_search",
            health_snapshot=snapshot,
        )
        self.assertEqual(advisory["recommended_provider_ids"][-1], "arxiv")
        self.assertEqual(advisory["blocked_count"], 0)

    def test_cooldown_provider_is_blocked(self):
        snapshot = _snapshot([
            _row(
                "arxiv",
                health="provider_cooldown",
                available_now=False,
                cooldown_active=True,
            ),
            _row("crossref"),
            _row("openalex"),
            _row("semantic_scholar"),
        ])
        advisory = build_router_advisory(
            self.root,
            capability="paper_search",
            health_snapshot=snapshot,
        )
        self.assertNotIn("arxiv", advisory["recommended_provider_ids"])
        self.assertIn("arxiv", advisory["blocked_provider_ids"])

    def test_health_missing_fails_closed(self):
        advisory = build_router_advisory(
            self.root,
            capability="repository_search",
            health_snapshot=_snapshot([]),
        )
        self.assertEqual(advisory["recommended_count"], 0)
        self.assertEqual(advisory["blocked_provider_ids"], ["github_public"])
        self.assertEqual(advisory["blocked"][0]["reason"], "health_missing")

    def test_unknown_capability_does_not_invent_provider(self):
        advisory = build_router_advisory(
            self.root,
            capability="does_not_exist",
            health_snapshot=_snapshot([]),
        )
        self.assertEqual(advisory["candidate_count"], 0)
        self.assertEqual(advisory["recommended_provider_ids"], [])

    def test_output_is_explicitly_advisory_and_non_mutating(self):
        advisory = build_router_advisory(
            self.root,
            capability="repository_search",
            health_snapshot=_snapshot([_row("github_public")]),
        )
        self.assertEqual(advisory["schema"], ADVISORY_SCHEMA)
        self.assertTrue(advisory["advisory_only"])
        self.assertFalse(advisory["routing_mutated"])
        self.assertEqual(advisory["api_requests"], 0)
        self.assertFalse(advisory["paid_spending"])

    def test_secret_value_is_never_serialized(self):
        secret = "never-show-this"
        advisory = build_router_advisory(
            self.root,
            capability="repository_search",
            environ={"SIRA_GITHUB_TOKEN": secret},
            health_snapshot=_snapshot([_row("github_public")]),
        )
        self.assertNotIn(secret, json.dumps(advisory, sort_keys=True))

    def test_cli_returns_nonzero_when_no_provider_is_recommended(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = main([
                "--root",
                str(self.root),
                "--capability",
                "does_not_exist",
            ])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 3)
        self.assertEqual(payload["recommended_count"], 0)

    def test_live_local_snapshot_contract_is_offline_and_nonspending(self):
        advisory = build_router_advisory(
            self.root,
            capability="paper_search",
            environ={},
        )
        self.assertEqual(advisory["api_requests"], 0)
        self.assertFalse(advisory["paid_spending"])
        self.assertTrue(advisory["advisory_only"])


if __name__ == "__main__":
    unittest.main()
