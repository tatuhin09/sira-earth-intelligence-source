from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.capability_broker import (
    STATUS_CAPABILITY_UNAVAILABLE,
    STATUS_CREDENTIAL_REQUIRED,
    STATUS_METERED_BUDGET_BLOCKED,
    STATUS_METERED_POLICY_BLOCKED,
    STATUS_READY,
    STATUS_TEMPORARILY_UNAVAILABLE,
    broker_decision,
)


def health(*rows):
    return {"schema": "sira.provider_health.v1", "providers": list(rows)}


def row(
    provider_id,
    *,
    health_name="ready",
    available=True,
    cost="free",
    policy_reason="ready",
    cooldown=False,
    unreliable=False,
):
    return {
        "provider_id": provider_id,
        "display_name": provider_id,
        "health": health_name,
        "available_now": available,
        "cost_class": cost,
        "historical_unreliable": unreliable,
        "cooldown_active": cooldown,
        "policy_reason": policy_reason,
    }


class CapabilityBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_free_ready_chain_is_deterministic(self):
        decision = broker_decision(
            self.root, "paper_search",
            health_snapshot=health(
                row("arxiv"), row("crossref"), row("openalex"), row("semantic_scholar")
            ),
        )
        self.assertEqual(decision["status"], STATUS_READY)
        self.assertEqual(
            [decision["selected_provider_id"], *decision["fallback_provider_ids"]],
            ["arxiv", "crossref", "openalex", "semantic_scholar"],
        )

    def test_unknown_capability_does_not_invent_provider_or_access_need(self):
        decision = broker_decision(self.root, "unknown_capability")
        self.assertEqual(decision["status"], STATUS_CAPABILITY_UNAVAILABLE)
        self.assertEqual(decision["candidate_provider_ids"], [])
        self.assertIsNone(decision["access_need"])
        self.assertFalse(decision["owner_action_required"])

    def test_metered_disabled_is_policy_block_not_owner_access(self):
        decision = broker_decision(
            self.root, "web_search", allow_metered=False,
            health_snapshot=health(
                row(
                    "tavily", health_name="policy_blocked", available=False,
                    cost="metered", policy_reason="metered_disabled",
                )
            ),
        )
        self.assertEqual(decision["status"], STATUS_METERED_POLICY_BLOCKED)
        self.assertIsNone(decision["access_need"])

    def test_required_credential_is_metadata_only_access_need(self):
        decision = broker_decision(
            self.root, "web_search", allow_metered=True,
            health_snapshot=health(
                row(
                    "tavily", health_name="policy_blocked", available=False,
                    cost="metered", policy_reason="required_credential_missing",
                )
            ),
        )
        self.assertEqual(decision["status"], STATUS_CREDENTIAL_REQUIRED)
        self.assertTrue(decision["owner_action_required"])
        self.assertFalse(decision["access_request_created"])
        self.assertEqual(decision["access_need"]["provider_id"], "tavily")
        self.assertEqual(decision["access_need"]["credential_name"], "TAVILY_API_KEY")

    def test_budget_and_cooldown_do_not_become_access_requests(self):
        budget = broker_decision(
            self.root, "web_search", allow_metered=True,
            environ={"TAVILY_API_KEY": "configured"},
            health_snapshot=health(
                row(
                    "tavily", health_name="metered_budget_blocked",
                    available=False, cost="metered",
                )
            ),
        )
        self.assertEqual(budget["status"], STATUS_METERED_BUDGET_BLOCKED)
        self.assertFalse(budget["owner_action_required"])

        temporary = broker_decision(
            self.root, "paper_search",
            health_snapshot=health(
                row("arxiv", health_name="provider_cooldown", available=False, cooldown=True),
                row("crossref", health_name="provider_cooldown", available=False, cooldown=True),
                row("openalex", health_name="provider_cooldown", available=False, cooldown=True),
                row("semantic_scholar", health_name="provider_cooldown", available=False, cooldown=True),
            ),
        )
        self.assertEqual(temporary["status"], STATUS_TEMPORARILY_UNAVAILABLE)
        self.assertFalse(temporary["owner_action_required"])

    def test_available_fallback_skips_cooldown_provider(self):
        decision = broker_decision(
            self.root, "paper_search",
            health_snapshot=health(
                row("arxiv", health_name="provider_cooldown", available=False, cooldown=True),
                row("crossref"), row("openalex"), row("semantic_scholar"),
            ),
        )
        self.assertEqual(decision["selected_provider_id"], "crossref")
        self.assertNotIn(
            "arxiv",
            [decision["selected_provider_id"], *decision["fallback_provider_ids"]],
        )

    def test_decision_never_serializes_environment_secret(self):
        decision = broker_decision(
            self.root, "web_search", allow_metered=True,
            environ={"TAVILY_API_KEY": "SUPER-SECRET-VALUE"},
            health_snapshot=health(row("tavily", cost="metered")),
        )
        rendered = json.dumps(decision, sort_keys=True)
        self.assertNotIn("SUPER-SECRET-VALUE", rendered)
        self.assertFalse(decision["provider_execution_performed"])
        self.assertFalse(decision["authority_granted"])
        self.assertFalse(decision["promotion_authorized"])

    def test_persisted_decision_is_local_audit_only(self):
        decision = broker_decision(
            self.root, "paper_search",
            health_snapshot=health(
                row("arxiv"), row("crossref"), row("openalex"), row("semantic_scholar")
            ),
            persist=True,
        )
        path = Path(decision["artifact"])
        self.assertTrue(path.is_file())
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["decision_id"], decision["decision_id"])
        self.assertFalse(persisted["access_request_created"])
        self.assertFalse(persisted["provider_execution_performed"])


if __name__ == "__main__":
    unittest.main()
