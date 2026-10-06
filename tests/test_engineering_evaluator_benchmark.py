from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_evaluator_benchmark import (
    engineering_evaluator_benchmark,
)


class EngineeringEvaluatorBenchmarkTests(unittest.TestCase):
    def test_offline_benchmark_is_fourteen_of_fourteen(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, report = engineering_evaluator_benchmark(Path(tmp))
        self.assertEqual(
            (
                report["passed"],
                report["failed"],
                report["api_requests"],
            ),
            (14, 0, 0),
        )


if __name__ == "__main__":
    unittest.main()
