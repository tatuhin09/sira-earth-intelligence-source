from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]; SRC=ROOT/"src"
sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.knowledge_consolidation_benchmark import knowledge_consolidation_benchmark
class KnowledgeConsolidationBenchmarkTests(unittest.TestCase):
    def test_offline_thirteen(self):
        with tempfile.TemporaryDirectory() as t: _,r=knowledge_consolidation_benchmark(Path(t))
        self.assertEqual((r["passed"],r["failed"],r["api_requests"]),(13,0,0))
if __name__=="__main__": unittest.main()
