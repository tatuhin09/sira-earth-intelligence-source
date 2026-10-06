from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [item for item in sys.path if item != str(SRC)]

from sira.research_mesh_benchmark import research_mesh_benchmark


class ResearchMeshBenchmarkTests(unittest.TestCase):
    def test_offline_research_mesh_benchmark_is_ten_of_ten(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, report = research_mesh_benchmark(Path(tmp))
        self.assertEqual(report["passed"], 10)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["api_requests"], 0)


if __name__ == "__main__":
    unittest.main()
