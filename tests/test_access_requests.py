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

from sira.access_requests import (
    AccessNeed,
    AccessRequestStore,
    KIND_CREDENTIAL,
    KIND_PAYMENT_APPROVAL,
    RISK_FINANCIAL,
    STATUS_APPROVED,
    STATUS_DENIED,
    STATUS_PENDING,
    STATUS_SATISFIED,
)


class AccessRequestStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = AccessRequestStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _credential_need(self) -> AccessNeed:
        return AccessNeed(
            kind=KIND_CREDENTIAL,
            resource="tavily",
            reason="Web search is blocked because the required credential is unavailable.",
            provider_id="tavily",
            credential_name="TAVILY_API_KEY",
            owner_action="Provision the credential through the approved secret channel.",
        )

    def test_create_persists_metadata_only_and_parks_task_reference(self):
        row = self.store.create(
            self._credential_need(),
            task_kind="multi_worker",
            task_id="mw_deadbeef",
            resume_hint="Retry provider selection after access becomes available.",
        )
        self.assertEqual(row["status"], STATUS_PENDING)
        self.assertTrue(row["owner_action_required"])
        self.assertTrue(row["continue_other_work"])
        self.assertFalse(row["secret_values_stored"])
        self.assertEqual(row["blocked_task"]["task_id"], "mw_deadbeef")
        encoded = json.dumps(row)
        self.assertNotIn('"secret":', encoded)
        self.assertNotIn('"password":', encoded)
        self.assertNotIn('"token":', encoded)
        self.assertNotIn('"api_key":', encoded)
        self.assertFalse(row["secret_values_stored"])

    def test_same_unresolved_need_is_deduplicated(self):
        first = self.store.create(self._credential_need(), task_kind="multi_worker", task_id="mw_same")
        second = self.store.create(self._credential_need(), task_kind="multi_worker", task_id="mw_same")
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(self.store.snapshot()["request_count"], 1)

    def test_distinct_task_gets_distinct_request(self):
        first = self.store.create(self._credential_need(), task_kind="multi_worker", task_id="mw_one")
        second = self.store.create(self._credential_need(), task_kind="multi_worker", task_id="mw_two")
        self.assertNotEqual(first["request_id"], second["request_id"])

    def test_owner_approval_does_not_claim_access_is_available(self):
        row = self.store.create(self._credential_need())
        approved = self.store.approve(row["request_id"])
        self.assertEqual(approved["status"], STATUS_APPROVED)
        self.assertIsNone(approved["satisfied_at"])
        self.assertEqual(self.store.snapshot()["resume_ready_count"], 0)

    def test_satisfied_request_releases_parked_task_once(self):
        row = self.store.create(self._credential_need(), task_kind="multi_worker", task_id="mw_resume")
        self.store.approve(row["request_id"])
        satisfied = self.store.mark_satisfied(row["request_id"])
        self.assertEqual(satisfied["status"], STATUS_SATISFIED)
        self.assertEqual(len(self.store.resume_ready()), 1)
        consumed = self.store.consume_resume(row["request_id"])
        self.assertIsNotNone(consumed["resume_consumed_at"])
        self.assertEqual(self.store.resume_ready(), ())

    def test_denied_request_never_becomes_resume_ready(self):
        row = self.store.create(self._credential_need(), task_kind="multi_worker", task_id="mw_denied")
        denied = self.store.deny(row["request_id"], note="Do not use this provider.")
        self.assertEqual(denied["status"], STATUS_DENIED)
        self.assertEqual(self.store.resume_ready(), ())

    def test_pending_notification_is_sanitized(self):
        row = self.store.create(self._credential_need(), task_kind="multi_worker", task_id="mw_notify")
        notice = self.store.pending_notifications()[0]
        self.assertEqual(notice["request_id"], row["request_id"])
        self.assertEqual(notice["credential_name"], "TAVILY_API_KEY")
        self.assertFalse(notice["contains_secret_value"])
        self.assertTrue(notice["continue_other_work"])

    def test_approval_removes_pending_notification(self):
        row = self.store.create(self._credential_need())
        self.assertEqual(len(self.store.pending_notifications()), 1)
        self.store.approve(row["request_id"])
        self.assertEqual(self.store.pending_notifications(), ())

    def test_payment_request_requires_financial_risk(self):
        with self.assertRaisesRegex(ValueError, "financial risk"):
            AccessNeed(
                kind=KIND_PAYMENT_APPROVAL,
                resource="service_trial",
                reason="A card-backed trial requires owner approval.",
            ).validate()

    def test_payment_approval_never_becomes_standing_authority(self):
        need = AccessNeed(
            kind=KIND_PAYMENT_APPROVAL,
            resource="service_trial",
            reason="A card-backed trial requires owner approval.",
            risk=RISK_FINANCIAL,
            owner_action="Approve this exact financial workflow.",
        )
        row = self.store.create(need)
        self.store.approve(row["request_id"])
        with self.assertRaisesRegex(ValueError, "standing autonomous authority"):
            self.store.mark_satisfied(row["request_id"])

    def test_invalid_fields_fail_closed(self):
        with self.assertRaises(ValueError):
            self.store.create(
                AccessNeed(
                    kind=KIND_CREDENTIAL,
                    resource="../escape",
                    reason="bad",
                    credential_name="TOKEN",
                )
            )
        with self.assertRaisesRegex(ValueError, "task_kind and task_id"):
            self.store.create(self._credential_need(), task_kind="multi_worker")

    def test_corrupt_or_symlinked_records_are_ignored(self):
        requests = self.root / "runtime" / "access_requests" / "requests"
        requests.mkdir(parents=True)
        corrupt = requests / ("ar_" + "0" * 32 + ".json")
        corrupt.write_text("{", encoding="utf-8")
        target = requests / "target.json"
        target.write_text("{}", encoding="utf-8")
        link = requests / ("ar_" + "1" * 32 + ".json")
        link.symlink_to(target)
        self.assertEqual(self.store.snapshot()["request_count"], 0)


if __name__ == "__main__":
    unittest.main()
