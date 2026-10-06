from pathlib import Path
import subprocess,sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/"src"))
from sira.improvement import SubprocessExperimentRunner

class FixtureRunner(SubprocessExperimentRunner):
    def _run(self,root,args):
        return subprocess.CompletedProcess(args,1,stdout="",stderr=
            'Traceback\n  File "tests/test_demo.py", line 9, in test_demo\nAssertionError: token=secret-value failed\nRan 1 test in 0.01s\nFAILED\n')

class ImprovementStructuredDiagnosticsTests(unittest.TestCase):
    def test_no_raw_output_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=FixtureRunner(timeout_seconds=2).evaluate(Path(tmp),None)
        self.assertNotIn("output_tail",repr(r))
        self.assertNotIn("secret-value",repr(r))
        self.assertEqual(r["tests"]["diagnostic_count"],1)
        self.assertFalse(r["overall_passed"])
