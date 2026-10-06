from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sira.models import ProviderError
from sira.runtime import _failed_worker_cycle
from sira.runtime_reliability import classify_cycle
from sira.worker_coordination import MultiWorkerCoordinator


class ProviderWorkerResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _readonly(_ctx, _target):
        return {
            "status": "completed",
            "api_requests": 0,
            "metered_model_requests": 0,
        }

    def _run_provider_failure(
        self,
        code,
        *,
        retry_after=None,
        request_count=1,
    ):
        def code_worker(_ctx, _target, _readonly):
            raise ProviderError(
                code,
                True,
                retry_after,
                request_count=request_count,
            )

        coordinator = MultiWorkerCoordinator(self.root)
        return coordinator.run_task(
            {
                "target_kind": "opportunity",
                "opportunity_id": "op_" + "a" * 32,
            },
            research_worker=self._readonly,
            verification_worker=self._readonly,
            code_worker=code_worker,
        )

    def test_provider_failure_preserves_sanitized_metadata(self):
        report = self._run_provider_failure(
            "http_429",
            retry_after=37,
            request_count=2,
        )
        self.assertEqual(report["status"], "failed_worker")
        detail = report["worker_failure_details"]["code"]
        self.assertEqual(detail["type"], "ProviderError")
        self.assertEqual(detail["code"], "http_429")
        self.assertTrue(detail["request_sent"])
        self.assertEqual(detail["retry_after"], 37)
        self.assertEqual(detail["request_count"], 2)
        self.assertNotIn("message", detail)
        self.assertEqual(
            report["worker_failures"]["code"],
            "ProviderError",
        )

    def test_network_provider_failure_classifies_transient(self):
        report = self._run_provider_failure(
            "network_or_timeout",
            request_count=1,
        )
        detail = report["worker_failure_details"]["code"]
        cycle = {
            "status": "failed",
            "outcome": "code_worker_failed",
            "error": detail,
        }
        self.assertEqual(
            classify_cycle(cycle),
            "transient_external",
        )

    def test_runtime_propagates_provider_error_without_raw_text(self):
        result = {
            "status": "running",
            "outcome": None,
            "resource_usage": {
                "free_public_api_requests": 3,
                "metered_model_requests": 0,
            },
        }
        coordination = {
            "status": "failed_worker",
            "outcome": "code_worker_failed",
            "worker_failure_details": {
                "code": {
                    "type": "ProviderError",
                    "code": "http_429",
                    "request_sent": True,
                    "retry_after": 41,
                    "request_count": 2,
                }
            },
        }
        mapped = _failed_worker_cycle(
            result,
            coordination,
        )
        self.assertEqual(mapped["status"], "failed")
        self.assertEqual(mapped["error"]["type"], "ProviderError")
        self.assertEqual(mapped["error"]["code"], "http_429")
        self.assertEqual(mapped["retry_after_seconds"], 41)
        self.assertEqual(
            mapped["resource_usage"]["metered_model_requests"],
            2,
        )
        self.assertNotIn("message", mapped["error"])
        self.assertEqual(
            classify_cycle(mapped),
            "transient_external",
        )

    def test_generic_code_bug_remains_internal(self):
        def code_worker(_ctx, _target, _readonly):
            raise RuntimeError("fixture internal bug")

        coordinator = MultiWorkerCoordinator(self.root)
        report = coordinator.run_task(
            {
                "target_kind": "opportunity",
                "opportunity_id": "op_" + "b" * 32,
            },
            research_worker=self._readonly,
            verification_worker=self._readonly,
            code_worker=code_worker,
        )
        self.assertEqual(report["status"], "failed_worker")
        self.assertNotIn(
            "code",
            report.get("worker_failure_details") or {},
        )
        cycle = {
            "status": "failed",
            "outcome": "code_worker_failed",
            "error": {
                "type": "MultiWorkerError",
                "message": "code_worker_failed",
            },
        }
        self.assertEqual(classify_cycle(cycle), "internal")


if __name__ == "__main__":
    unittest.main()
