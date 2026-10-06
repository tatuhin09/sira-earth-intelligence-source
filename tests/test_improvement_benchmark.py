import json
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.improvement_benchmark import improvement_benchmark


class ImprovementBenchmarkTests(unittest.TestCase):
    def test_offline_improvement_benchmark_has_four_cases_and_zero_api_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runs").mkdir()
            path, report = improvement_benchmark(root)
            self.assertTrue(path.is_file())
            self.assertEqual(report["passed"], 4)
            self.assertEqual(report["failed"], 0)
            self.assertEqual(report["api_requests"], 0)
            self.assertEqual(len(report["cases"]), 4)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["kind"],
                             "synthetic_improvement_loop_regression_not_live_self_modification")


if __name__ == "__main__":
    unittest.main()
