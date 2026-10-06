from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SRC_TEXT = str(SRC)
sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]

from sira.evolution_benchmark import evolution_benchmark


class EvolutionBenchmarkTests(unittest.TestCase):
    def test_offline_evolution_benchmark_has_six_cases_and_zero_api_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "benchmarks").mkdir()
            source = ROOT / "benchmarks/evolution_cases.json"
            (root / "benchmarks/evolution_cases.json").write_bytes(source.read_bytes())
            _, report = evolution_benchmark(root)
        self.assertEqual(report["passed"], 6)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["api_requests"], 0)


if __name__ == "__main__":
    unittest.main()
