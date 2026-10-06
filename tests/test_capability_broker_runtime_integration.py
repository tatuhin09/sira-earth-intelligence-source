from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.opportunity_research import _default_provider_plan, _default_providers


class CapabilityBrokerRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def decision(self, *chain, status="ready", access_need=None):
        return {
            "schema": "sira.capability_broker_decision.v1",
            "policy_version": 1,
            "decision_id": "cb_" + "a" * 32,
            "capability": "paper_search",
            "status": status,
            "selected_provider_id": chain[0] if chain else None,
            "fallback_provider_ids": list(chain[1:]),
            "candidate_provider_ids": list(chain),
            "access_need": access_need,
            "owner_action_required": access_need is not None,
            "allow_metered": False,
            "api_requests": 0,
            "paid_spending": False,
            "provider_execution_performed": False,
            "access_request_created": False,
            "authority_granted": False,
            "promotion_authorized": False,
            "artifact": str(self.root / "runtime/capability_broker/decisions/cb_test.json"),
        }

    def test_default_consumer_order_follows_broker_chain(self):
        with patch(
            "sira.capability_broker.broker_decision",
            return_value=self.decision("openalex", "arxiv", "crossref", "semantic_scholar"),
        ):
            names = [provider.name for provider in _default_providers(self.root)]
        self.assertEqual(names, ["openalex", "arxiv", "crossref"])

    def test_catalog_provider_not_supported_by_consumer_is_not_executed(self):
        with patch(
            "sira.capability_broker.broker_decision",
            return_value=self.decision("semantic_scholar", "crossref", "openalex", "arxiv"),
        ):
            providers, audit = _default_provider_plan(self.root)
        self.assertEqual([provider.name for provider in providers], ["crossref", "openalex", "arxiv"])
        self.assertEqual(audit["consumer_unsupported_provider_ids"], ["semantic_scholar"])
        self.assertNotIn("semantic_scholar", audit["consumer_provider_chain"])

    def test_broker_unavailable_fails_closed_without_hardcoded_resurrection(self):
        with patch(
            "sira.capability_broker.broker_decision",
            return_value=self.decision(status="temporarily_unavailable"),
        ):
            providers, audit = _default_provider_plan(self.root)
        self.assertEqual(providers, ())
        self.assertTrue(audit["fail_closed"])
        self.assertFalse(audit["provider_execution_performed"])

    def test_broker_credential_need_remains_metadata_only(self):
        need = {
            "kind": "credential",
            "resource": "provider:tavily",
            "reason": "fixture",
            "risk": "low",
            "provider_id": "tavily",
            "credential_name": "TAVILY_API_KEY",
            "owner_action": "configure locally",
        }
        with patch(
            "sira.capability_broker.broker_decision",
            return_value=self.decision(status="credential_required", access_need=need),
        ):
            providers, audit = _default_provider_plan(self.root)
        self.assertEqual(providers, ())
        self.assertEqual(audit["access_need"]["credential_name"], "TAVILY_API_KEY")
        self.assertFalse(audit["access_request_created"])

    def test_audit_is_bounded_non_authoritative_and_secret_free(self):
        mocked = {
            **self.decision("arxiv", "crossref", "openalex"),
            "recommendations": [{"provider_id": "arxiv", "learned_adjustment": 0.1}],
            "blocked": [{"provider_id": "tavily", "reason": "missing secret"}],
            "secret": "DO-NOT-KEEP",
        }
        with patch("sira.capability_broker.broker_decision", return_value=mocked):
            _, audit = _default_provider_plan(self.root)
        rendered = json.dumps(audit, sort_keys=True)
        self.assertNotIn("DO-NOT-KEEP", rendered)
        self.assertNotIn("recommendations", audit)
        self.assertNotIn("blocked", audit)
        self.assertFalse(audit["authority_granted"])
        self.assertFalse(audit["promotion_authorized"])


if __name__ == "__main__":
    unittest.main()
