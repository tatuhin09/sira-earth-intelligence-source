from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/"src"
sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.language_learning_benchmark import language_learning_benchmark

class LanguageLearningBenchmarkTests(unittest.TestCase):
    def test_offline_benchmark_is_eleven_of_eleven(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,r=language_learning_benchmark(Path(tmp))
        self.assertEqual((r["passed"],r["failed"],r["api_requests"]),(11,0,0))

if __name__=="__main__": unittest.main()
