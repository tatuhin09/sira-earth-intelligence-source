from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/"src"))
from sira.engineering_diagnostics_benchmark import engineering_diagnostics_benchmark
class EngineeringDiagnosticsBenchmarkTests(unittest.TestCase):
    def test_offline_twelve(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,r=engineering_diagnostics_benchmark(Path(tmp))
        self.assertEqual((r["passed"],r["failed"],r["api_requests"]),(12,0,0))
