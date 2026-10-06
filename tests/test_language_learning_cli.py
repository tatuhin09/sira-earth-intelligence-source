import io,json
from contextlib import redirect_stdout
from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/"src"
sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.cli import main

class LanguageLearningCliTests(unittest.TestCase):
    def test_language_analyze_and_learning_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            out=io.StringIO()
            with redirect_stdout(out):
                rc=main(["--root",str(root),"language","analyze",
                         "ami akhon code korbo amk bolo"])
            self.assertEqual(rc,0)
            self.assertEqual(json.loads(out.getvalue())["language_label"],"bn-Latn-banglish")
            out=io.StringIO()
            with redirect_stdout(out):
                rc=main(["--root",str(root),"learning","stats"])
            self.assertEqual(rc,0)
            self.assertEqual(json.loads(out.getvalue())["schema_version"],1)

if __name__=="__main__": unittest.main()
