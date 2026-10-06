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

from sira.provider_policy import (
    REASON_METERED_DISABLED,
    REASON_REQUIRED_CREDENTIAL_MISSING,
    build_provider_plan,
    main,
)


class ProviderPolicyTests(unittest.TestCase):
    def test_paper_search_prefers_free_ready_providers_deterministically(self):
        plan = build_provider_plan(
            capability="paper_search",
            environ={},
        )

        self.assertEqual(
            plan["selected_provider_ids"],
            ["arxiv", "crossref", "openalex", "semantic_scholar"],
        )
        self.assertEqual(plan["selected_count"], 4)
        self.assertEqual(plan["blocked_count"], 0)

    def test_github_public_is_usable_without_optional_token(self):
        plan = build_provider_plan(
            capability="repository_search",
            environ={},
        )

        self.assertEqual(plan["selected_provider_ids"], ["github_public"])
        decision = plan["decisions"][0]
        self.assertFalse(decision["credential_required"])
        self.assertFalse(decision["credential_configured"])
        self.assertTrue(decision["selected"])

    def test_metered_provider_is_blocked_by_default_even_with_key(self):
        plan = build_provider_plan(
            capability="web_search",
            environ={"TAVILY_API_KEY": "secret-value"},
        )

        self.assertEqual(plan["selected_count"], 0)
        decision = plan["decisions"][0]
        self.assertEqual(decision["provider_id"], "tavily")
        self.assertEqual(decision["reason"], REASON_METERED_DISABLED)

    def test_metered_provider_still_blocks_without_required_key_when_enabled(self):
        plan = build_provider_plan(
            capability="web_search",
            allow_metered=True,
            environ={},
        )

        self.assertEqual(plan["selected_count"], 0)
        decision = plan["decisions"][0]
        self.assertEqual(
            decision["reason"],
            REASON_REQUIRED_CREDENTIAL_MISSING,
        )

    def test_metered_provider_can_be_selected_only_when_explicitly_enabled_and_ready(self):
        secret = "must-not-leak"
        plan = build_provider_plan(
            capability="web_search",
            allow_metered=True,
            environ={"TAVILY_API_KEY": secret},
        )

        self.assertEqual(plan["selected_provider_ids"], ["tavily"])
        serialized = json.dumps(plan, sort_keys=True)
        self.assertNotIn(secret, serialized)

    def test_unknown_capability_is_empty_not_invented(self):
        plan = build_provider_plan(
            capability="does_not_exist",
            environ={},
        )

        self.assertEqual(plan["candidate_count"], 0)
        self.assertEqual(plan["selected_count"], 0)
        self.assertEqual(plan["decisions"], [])

    def test_cli_outputs_json_and_nonzero_when_no_provider_is_selectable(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = main(["--capability", "does_not_exist"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 3)
        self.assertEqual(payload["selected_count"], 0)
        self.assertEqual(payload["schema"], "sira.provider_policy.v1")

    def test_cli_default_does_not_enable_metered_provider(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = main(["--capability", "web_search"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 3)
        self.assertEqual(payload["decisions"][0]["provider_id"], "tavily")
        self.assertFalse(payload["filters"]["allow_metered"])


if __name__ == "__main__":
    unittest.main()
