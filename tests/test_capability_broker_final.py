from __future__ import annotations

import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.access_runtime import access_need_from_broker_decision
from sira.capability_broker import (
    STATUS_CREDENTIAL_REQUIRED,
    STATUS_METERED_POLICY_BLOCKED,
    STATUS_READY,
    broker_decision,
)
from sira.capability_broker_final_benchmark import capability_broker_final_benchmark
from sira.papers import papers_run
from sira.provider_catalog import ACCESS_KEY_REQUIRED, COST_METERED, get_provider
from sira.reading import read_run
from sira.synthesis import answer_run


class CapabilityBrokerFinalContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_gemini_is_formal_metered_key_required_capability(self):
        descriptor = get_provider("gemini")
        self.assertEqual(descriptor.access, ACCESS_KEY_REQUIRED)
        self.assertEqual(descriptor.cost_class, COST_METERED)
        self.assertEqual(descriptor.credential_env, ("GEMINI_API_KEY",))
        self.assertIn("code_generation", descriptor.capabilities)
        self.assertIn("evidence_synthesis", descriptor.capabilities)
        self.assertTrue(descriptor.read_only)
        self.assertFalse(descriptor.executes_remote_code)

    def test_code_generation_is_metered_disabled_by_default(self):
        decision = broker_decision(
            self.root,
            "code_generation",
            allow_metered=False,
            environ={"GEMINI_API_KEY": "configured"},
        )
        self.assertEqual(decision["status"], STATUS_METERED_POLICY_BLOCKED)
        self.assertIsNone(decision["access_need"])

    def test_missing_key_becomes_metadata_only_access_need(self):
        decision = broker_decision(
            self.root,
            "code_generation",
            allow_metered=True,
            environ={},
        )
        self.assertEqual(decision["status"], STATUS_CREDENTIAL_REQUIRED)
        self.assertFalse(decision["access_request_created"])
        self.assertEqual(decision["access_need"]["provider_id"], "gemini")
        self.assertEqual(decision["access_need"]["credential_name"], "GEMINI_API_KEY")
        need = access_need_from_broker_decision(decision)
        self.assertIsNotNone(need)
        self.assertEqual(need.provider_id, "gemini")

    def test_configured_placeholder_never_leaks_into_broker_audit(self):
        decision = broker_decision(
            self.root,
            "code_generation",
            allow_metered=True,
            environ={"GEMINI_API_KEY": "THIS-IS-PRESENCE-ONLY"},
        )
        self.assertEqual(decision["status"], STATUS_READY)
        self.assertEqual(decision["selected_provider_id"], "gemini")
        self.assertNotIn("THIS-IS-PRESENCE-ONLY", json.dumps(decision, sort_keys=True))

    def test_explicit_dependency_consumers_remain_explicit(self):
        self.assertIs(
            inspect.signature(papers_run).parameters["provider"].default,
            inspect.Parameter.empty,
        )
        self.assertIs(
            inspect.signature(read_run).parameters["reader"].default,
            inspect.Parameter.empty,
        )
        self.assertIs(
            inspect.signature(answer_run).parameters["model"].default,
            inspect.Parameter.empty,
        )

    def test_offline_final_benchmark_has_ten_cases_and_zero_api_requests(self):
        _, report = capability_broker_final_benchmark(self.root)
        self.assertEqual(report["passed"], 10)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["api_requests"], 0)


if __name__ == "__main__":
    unittest.main()
