from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [item for item in sys.path if item != str(SRC)]

from sira.gemini_research_benchmark import gemini_research_benchmark


class GeminiResearchBenchmarkTests(unittest.TestCase):
    def test_offline_benchmark_is_eight_of_eight(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, report = gemini_research_benchmark(Path(tmp))
        self.assertEqual(report["passed"], 8)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["api_requests"], 0)


if __name__ == "__main__":
    unittest.main()
