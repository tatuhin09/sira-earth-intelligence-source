from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.language_semantic_benchmark import language_semantic_benchmark


class LanguageSemanticBenchmarkTests(unittest.TestCase):
    def test_offline_benchmark_is_ten_of_ten(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, report = language_semantic_benchmark(Path(tmp))
        self.assertEqual(
            (report["passed"], report["failed"], report["api_requests"]),
            (10, 0, 0),
        )


if __name__ == "__main__":
    unittest.main()
