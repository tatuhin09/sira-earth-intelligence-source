from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SRC_TEXT = str(SRC)
sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]

from sira.access_provisioning import (
    reconcile_approved_access_requests,
    verify_and_satisfy_access_request,
    verify_credential_request,
)
from sira.access_requests import (
    AccessNeed,
    AccessRequestStore,
    KIND_CREDENTIAL,
    STATUS_APPROVED,
    STATUS_SATISFIED,
)


class AccessProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = AccessRequestStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _request(self, credential_name="TAVILY_API_KEY"):
        row = self.store.create(
            AccessNeed(
                kind=KIND_CREDENTIAL,
                resource="tavily",
                reason="Web search requires provider access.",
                provider_id="tavily",
                credential_name=credential_name,
                owner_action="Provision the credential locally.",
            ),
            task_kind="autonomous_target",
            task_id="opportunity:op_" + "a" * 32,
        )
        return self.store.approve(row["request_id"])

    def test_missing_local_credential_does_not_satisfy_request(self):
        row = self._request()
        result = verify_and_satisfy_access_request(self.root, row["request_id"])

        self.assertFalse(result["satisfied"])
        self.assertEqual(result["verification"]["reason"], "credential_missing")
        self.assertEqual(self.store.load(row["request_id"])["status"], STATUS_APPROVED)

    def test_local_env_credential_is_verified_without_returning_value(self):
        row = self._request()
        secret = "tavily-local-test-key"
        (self.root / ".env").write_text(
            f"TAVILY_API_KEY={secret}\n",
            encoding="utf-8",
        )

        result = verify_and_satisfy_access_request(self.root, row["request_id"])
        encoded = json.dumps(result, sort_keys=True)

        self.assertEqual(result["status"], STATUS_SATISFIED)
        self.assertTrue(result["verified_satisfaction"])
        self.assertNotIn(secret, encoded)
        self.assertFalse(result["secret_value_returned"])

    def test_process_environment_credential_is_supported_without_persistence(self):
        import os
        from unittest.mock import patch

        row = self._request()
        with patch.dict(os.environ, {"TAVILY_API_KEY": "env-only-key"}, clear=False):
            result = verify_credential_request(self.root, row["request_id"])

        self.assertTrue(result["verified"])
        self.assertEqual(result["reason"], "credential_configured")
        self.assertFalse((self.root / ".env").exists())

    def test_unsupported_credential_verifier_fails_closed(self):
        row = self._request("UNSUPPORTED_TOKEN")
        result = verify_and_satisfy_access_request(self.root, row["request_id"])

        self.assertFalse(result["satisfied"])
        self.assertEqual(
            result["verification"]["reason"],
            "unsupported_credential_verifier",
        )
        self.assertEqual(self.store.load(row["request_id"])["status"], STATUS_APPROVED)

    def test_reconcile_satisfies_only_verified_approved_credentials(self):
        verified = self._request()
        second = self.store.create(
            AccessNeed(
                kind=KIND_CREDENTIAL,
                resource="other",
                reason="Other credential needed.",
                provider_id="other",
                credential_name="GEMINI_API_KEY",
                owner_action="Provision locally.",
            ),
            task_kind="autonomous_target",
            task_id="opportunity:op_" + "b" * 32,
        )
        waiting = self.store.approve(second["request_id"])

        (self.root / ".env").write_text(
            "TAVILY_API_KEY=available-key\n",
            encoding="utf-8",
        )
        result = reconcile_approved_access_requests(self.root)

        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["verified_satisfied"], 1)
        self.assertEqual(result["waiting_provision"], 1)
        self.assertEqual(self.store.load(verified["request_id"])["status"], STATUS_SATISFIED)
        self.assertEqual(self.store.load(waiting["request_id"])["status"], STATUS_APPROVED)

    def test_verification_requires_owner_approval(self):
        row = self.store.create(
            AccessNeed(
                kind=KIND_CREDENTIAL,
                resource="tavily",
                reason="Need access.",
                provider_id="tavily",
                credential_name="TAVILY_API_KEY",
                owner_action="Provision locally.",
            )
        )
        with self.assertRaisesRegex(ValueError, "owner-approved"):
            verify_credential_request(self.root, row["request_id"])


if __name__ == "__main__":
    unittest.main()
