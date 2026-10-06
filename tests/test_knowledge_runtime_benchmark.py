from pathlib import Path
import sys, tempfile, unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sira.knowledge_runtime_benchmark import knowledge_runtime_benchmark

class KnowledgeRuntimeBenchmarkTests(unittest.TestCase):
    def test_offline_twelve(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, report=knowledge_runtime_benchmark(Path(tmp))
        self.assertEqual((report["passed"],report["failed"],report["api_requests"]),(12,0,0))
        self.assertIn({"case_id":"before_proactive_opportunity","passed":True}, report["cases"])
