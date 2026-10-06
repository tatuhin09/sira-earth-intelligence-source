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

from sira.provider_diagnostics import (
    DIAGNOSTICS_SCHEMA,
    build_provider_diagnostics,
)


class ProviderDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_diagnostics_is_read_only_nonspending_and_nonrouting(self):
        payload = build_provider_diagnostics(
            self.root,
            environ={},
        )

        self.assertEqual(payload["schema"], DIAGNOSTICS_SCHEMA)
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["routing_mutated"])
        self.assertEqual(payload["api_requests"], 0)
        self.assertFalse(payload["paid_spending"])

    def test_summary_matches_current_catalog_size(self):
        payload = build_provider_diagnostics(
            self.root,
            environ={},
        )

        self.assertEqual(payload["summary"]["provider_count"], 11)
        self.assertEqual(payload["catalog"]["provider_count"], 11)
        self.assertEqual(payload["readiness"]["provider_count"], 11)

    def test_current_empty_environment_blocks_tavily_only(self):
        payload = build_provider_diagnostics(
            self.root,
            environ={},
        )

        self.assertEqual(
            payload["summary"]["blocked_provider_ids"],
            ["gemini", "tavily"],
        )
        self.assertEqual(payload["summary"]["available_count"], 9)

    def test_advisories_cover_catalog_capabilities(self):
        payload = build_provider_diagnostics(
            self.root,
            environ={},
        )

        advisories = payload["advisories"]
        self.assertIn("paper_search", advisories)
        self.assertIn("repository_search", advisories)
        self.assertIn("web_search", advisories)
        self.assertEqual(
            advisories["paper_search"]["recommended_provider_ids"],
            ["arxiv", "crossref", "openalex", "semantic_scholar"],
        )

    def test_web_search_advisory_is_blocked_without_required_key(self):
        payload = build_provider_diagnostics(
            self.root,
            environ={},
        )

        web = payload["advisories"]["web_search"]
        self.assertEqual(web["recommended_count"], 0)
        self.assertEqual(web["blocked_provider_ids"], ["tavily"])

    def test_optional_credentials_do_not_block_free_providers(self):
        payload = build_provider_diagnostics(
            self.root,
            environ={},
        )

        repo = payload["advisories"]["repository_search"]
        papers = payload["advisories"]["paper_search"]
        self.assertEqual(repo["recommended_provider_ids"], ["github_public"])
        self.assertIn("semantic_scholar", papers["recommended_provider_ids"])

    def test_secret_values_never_appear_in_consolidated_payload(self):
        secret = "secret-must-not-leak"
        payload = build_provider_diagnostics(
            self.root,
            allow_metered=True,
            environ={
                "TAVILY_API_KEY": secret,
                "SIRA_GITHUB_TOKEN": secret,
                "SIRA_SEMANTIC_SCHOLAR_API_KEY": secret,
            },
        )

        self.assertNotIn(secret, json.dumps(payload, sort_keys=True))

    def test_metered_eligibility_still_uses_existing_runtime_budget(self):
        payload = build_provider_diagnostics(
            self.root,
            allow_metered=True,
            environ={"TAVILY_API_KEY": "present"},
        )

        tavily = next(
            row
            for row in payload["health"]["providers"]
            if row["provider_id"] == "tavily"
        )
        self.assertIn(
            tavily["health"],
            {"ready", "metered_budget_blocked", "degraded_history"},
        )

    def test_payload_is_json_serializable(self):
        payload = build_provider_diagnostics(
            self.root,
            environ={},
        )
        encoded = json.dumps(payload, sort_keys=True)
        self.assertIn(DIAGNOSTICS_SCHEMA, encoded)


if __name__ == "__main__":
    unittest.main()
