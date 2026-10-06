from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sira.provider_catalog import (
    ACCESS_KEY_REQUIRED,
    COST_FREE,
    COST_METERED,
    PROVIDER_CATALOG,
    ProviderCatalogError,
    ProviderDescriptor,
    catalog_snapshot,
    free_first,
    get_provider,
    list_providers,
    main,
    validate_descriptor,
)


class ProviderCatalogTests(unittest.TestCase):
    def test_catalog_contains_current_public_research_provider_foundation(self):
        expected = {
            "github_public",
            "openalex",
            "crossref",
            "arxiv",
            "semantic_scholar",
            "tavily",
            "wikimedia",
            "gemini",
            "doaj",
            "europe_pmc",
            "pubmed",
        }
        self.assertEqual(set(PROVIDER_CATALOG), expected)

    def test_catalog_is_read_only_and_never_declares_remote_code_execution(self):
        for descriptor in PROVIDER_CATALOG.values():
            with self.subTest(provider=descriptor.provider_id):
                self.assertTrue(descriptor.read_only)
                self.assertFalse(descriptor.executes_remote_code)

    def test_github_public_contract_matches_bounded_reader(self):
        github = get_provider("github_public")

        self.assertIn("repository_search", github.capabilities)
        self.assertIn("public_file_read", github.capabilities)
        self.assertEqual(github.cost_class, COST_FREE)
        self.assertIn("SIRA_GITHUB_TOKEN", github.credential_env)
        self.assertTrue(github.autonomous_research_allowed)

    def test_tavily_is_marked_key_required_and_metered(self):
        tavily = get_provider("tavily")

        self.assertEqual(tavily.access, ACCESS_KEY_REQUIRED)
        self.assertEqual(tavily.cost_class, COST_METERED)
        self.assertEqual(tavily.credential_env, ("TAVILY_API_KEY",))

    def test_filters_are_deterministic_and_do_not_invent_providers(self):
        scholarly = list_providers(domain="scholarly")
        ids = [item.provider_id for item in scholarly]

        self.assertEqual(ids, sorted(ids))
        self.assertEqual(
            set(ids),
            {"arxiv", "crossref", "doaj", "europe_pmc", "openalex", "pubmed", "semantic_scholar"},
        )

        paper_search = list_providers(capability="paper_search")
        self.assertEqual(
            {item.provider_id for item in paper_search},
            {"arxiv", "crossref", "openalex", "semantic_scholar"},
        )

    def test_free_first_puts_metered_provider_after_free_providers(self):
        ordered = free_first(
            [
                get_provider("tavily"),
                get_provider("github_public"),
                get_provider("openalex"),
            ]
        )

        self.assertEqual(ordered[-1].provider_id, "tavily")
        self.assertTrue(
            all(item.cost_class == COST_FREE for item in ordered[:-1])
        )

    def test_invalid_descriptor_fails_closed(self):
        with self.assertRaises(ProviderCatalogError):
            validate_descriptor(
                ProviderDescriptor(
                    provider_id="bad provider",
                    display_name="Bad",
                    domains=("web",),
                    capabilities=("search",),
                    access="public",
                    cost_class="free",
                )
            )

        with self.assertRaises(ProviderCatalogError):
            validate_descriptor(
                ProviderDescriptor(
                    provider_id="unsafe",
                    display_name="Unsafe",
                    domains=("software",),
                    capabilities=("execute",),
                    access="public",
                    cost_class="free",
                    executes_remote_code=True,
                )
            )

    def test_snapshot_and_cli_are_json_serializable(self):
        snapshot = catalog_snapshot()
        self.assertEqual(snapshot["schema"], "sira.provider_catalog.v1")
        self.assertEqual(snapshot["provider_count"], 11)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = main()

        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["provider_count"], 11)
        self.assertEqual(
            [item["provider_id"] for item in payload["providers"]],
            sorted(PROVIDER_CATALOG),
        )


if __name__ == "__main__":
    unittest.main()
