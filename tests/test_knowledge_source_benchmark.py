from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]; SRC=ROOT/"src"
sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.knowledge_source_benchmark import knowledge_source_benchmark
class KnowledgeSourceBenchmarkTests(unittest.TestCase):
    def test_offline_twelve(self):
        with tempfile.TemporaryDirectory() as t:
            _,r=knowledge_source_benchmark(Path(t))
        self.assertEqual((r["passed"],r["failed"],r["api_requests"]),(12,0,0))
