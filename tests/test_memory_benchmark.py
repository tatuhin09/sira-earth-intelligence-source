import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.cli import main
from sira.memory_benchmark import memory_benchmark


class MemoryBenchmarkTests(unittest.TestCase):
    def test_offline_memory_benchmark_covers_transitive_clusters_without_api_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, report = memory_benchmark(root)
            self.assertEqual(report["passed"], 25)
            self.assertEqual(report["failed"], 0)
            self.assertEqual(report["api_requests"], 0)
            self.assertEqual(len(report["cases"]), 25)
            transitive = next(
                case for case in report["cases"]
                if case["case_id"] == "transitive_cluster_chain"
            )
            self.assertTrue(transitive["passed"])
            self.assertTrue(path.is_file())

            out = io.StringIO()
            with redirect_stdout(out):
                rc = main(["--root", str(root), "benchmark", "--memory"])
            self.assertEqual(rc, 0)
            cli = json.loads(out.getvalue())
            self.assertEqual(cli["status"], "passed")
            self.assertEqual(cli["passed"], 25)
            self.assertEqual(cli["api_requests"], 0)


if __name__ == "__main__":
    unittest.main()
