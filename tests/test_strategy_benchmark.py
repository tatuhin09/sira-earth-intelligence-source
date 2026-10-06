from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sira.strategy_benchmark import strategy_benchmark


class StrategyBenchmarkTests(unittest.TestCase):
    def test_offline_strategy_benchmark_has_eight_cases_and_zero_api_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runs").mkdir()
            path, report = strategy_benchmark(root)
            self.assertTrue(path.is_file())
            self.assertEqual(report["passed"], 8)
            self.assertEqual(report["failed"], 0)
            self.assertEqual(report["api_requests"], 0)
            self.assertEqual(len(report["cases"]), 8)


if __name__ == "__main__":
    unittest.main()
