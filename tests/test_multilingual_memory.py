from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/"src"
sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.memory import MemoryStore,Observation

class MultilingualMemoryTests(unittest.TestCase):
    def test_bengali_terms_participate_in_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MemoryStore(Path(tmp))
            mid,_=store.upsert(
                Observation("success","success","language_learning",
                    "বাংলা ভাষা বোঝার যাচাইকৃত সাফল্য","validated",
                    provider="local_language",signature="bn-success"),
                run_id="langunicode000000000000000000000001",
                artifact_name="language-learning.json",
                artifact_sha256="a"*64,outcome_status="completed")
            rows=store.retrieve("বাংলা ভাষা",limit=5)
            self.assertTrue(rows)
            self.assertEqual(rows[0]["memory_id"],mid)
            relevance=next(r for r in rows[0]["score_reasons"] if r["component"]=="relevance")
            self.assertIn("বাংলা",relevance["matched_terms"])
            self.assertIn("ভাষা",relevance["matched_terms"])

if __name__=="__main__": unittest.main()
