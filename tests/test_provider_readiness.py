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

from sira.provider_catalog import get_provider
from sira.provider_readiness import (
    STATE_OPTIONAL_CREDENTIAL_MISSING,
    STATE_READY,
    STATE_REQUIRED_CREDENTIAL_MISSING,
    assess_provider,
    main,
    readiness_snapshot,
)


class ProviderReadinessTests(unittest.TestCase):
    def test_public_provider_is_ready_without_credentials(self):
        row = assess_provider(get_provider("openalex"), environ={})

        self.assertTrue(row.usable)
        self.assertEqual(row.state, STATE_READY)
        self.assertFalse(row.credential_required)
        self.assertFalse(row.credential_configured)

    def test_optional_key_provider_remains_usable_when_key_is_missing(self):
        row = assess_provider(get_provider("github_public"), environ={})

        self.assertTrue(row.usable)
        self.assertEqual(row.state, STATE_OPTIONAL_CREDENTIAL_MISSING)
        self.assertFalse(row.credential_required)
        self.assertFalse(row.credential_configured)

    def test_required_key_provider_blocks_when_key_is_missing(self):
        row = assess_provider(get_provider("tavily"), environ={})

        self.assertFalse(row.usable)
        self.assertEqual(row.state, STATE_REQUIRED_CREDENTIAL_MISSING)
        self.assertTrue(row.credential_required)
        self.assertFalse(row.credential_configured)

    def test_required_key_provider_becomes_ready_when_key_is_present(self):
        row = assess_provider(
            get_provider("tavily"),
            environ={"TAVILY_API_KEY": "super-secret-value"},
        )

        self.assertTrue(row.usable)
        self.assertEqual(row.state, STATE_READY)
        self.assertTrue(row.credential_configured)

    def test_snapshot_never_exposes_secret_values(self):
        secret = "secret-that-must-never-appear"
        snapshot = readiness_snapshot(
            environ={
                "TAVILY_API_KEY": secret,
                "SIRA_GITHUB_TOKEN": secret,
                "SIRA_SEMANTIC_SCHOLAR_API_KEY": secret,
            }
        )
        serialized = json.dumps(snapshot, sort_keys=True)

        self.assertNotIn(secret, serialized)
        self.assertIn("TAVILY_API_KEY", serialized)
        self.assertEqual(snapshot["credential_configured_count"], 3)

    def test_full_snapshot_is_deterministic_and_catalog_complete(self):
        snapshot = readiness_snapshot(environ={})
        ids = [item["provider_id"] for item in snapshot["providers"]]

        self.assertEqual(snapshot["schema"], "sira.provider_readiness.v1")
        self.assertEqual(snapshot["provider_count"], 11)
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(snapshot["blocked_count"], 2)
        self.assertEqual(snapshot["usable_count"], 9)

    def test_single_provider_snapshot_is_supported(self):
        snapshot = readiness_snapshot(
            environ={},
            provider_id="github_public",
        )

        self.assertEqual(snapshot["provider_count"], 1)
        self.assertEqual(
            snapshot["providers"][0]["provider_id"],
            "github_public",
        )

    def test_cli_unknown_provider_returns_safe_json(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = main(["--provider", "does_not_exist"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 2)
        self.assertEqual(payload["error"], "unknown_provider")
        self.assertNotIn("Traceback", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
