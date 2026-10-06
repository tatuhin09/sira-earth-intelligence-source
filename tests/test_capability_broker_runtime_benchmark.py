from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.capability_broker_runtime_benchmark import capability_broker_runtime_benchmark


class CapabilityBrokerRuntimeBenchmarkTests(unittest.TestCase):
    def test_offline_runtime_broker_benchmark_has_eight_cases_and_zero_api_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, report = capability_broker_runtime_benchmark(Path(tmp))
        self.assertEqual(report["passed"], 8)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["api_requests"], 0)


if __name__ == "__main__":
    unittest.main()
