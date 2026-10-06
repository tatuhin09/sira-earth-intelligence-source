from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/"src"
sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.learning_consolidation import LearningConsolidationStore

class LearningConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=LearningConsolidationStore(Path(self.tmp.name))
    def tearDown(self): self.tmp.cleanup()

    def test_unverified_or_low_confidence_mapping_does_not_enter_learning(self):
        self.assertFalse(self.store.record_language_mapping("korbo","will do",
            evidence_id="e:a",confidence=.99,verifier="v",verified=False))
        self.assertFalse(self.store.record_language_mapping("korbo","will do",
            evidence_id="e:b",confidence=.3,verifier="v",verified=True))
        self.assertEqual(self.store.stats()["language_mapping_evidence"],0)

    def test_mapping_threshold_and_conflict(self):
        for eid in ("e:a","e:b"):
            self.store.record_language_mapping("korbo","will do",evidence_id=eid,
                confidence=.92,verifier="v",verified=True)
        self.assertEqual(self.store.consolidate_language_mapping("korbo").status,"consolidated")
        self.store.record_language_mapping("amr","my",evidence_id="e:c",
            confidence=.95,verifier="v",verified=True)
        self.store.record_language_mapping("amr","our",evidence_id="e:d",
            confidence=.95,verifier="v",verified=True)
        self.assertEqual(self.store.consolidate_language_mapping("amr").status,"blocked")

    def test_skill_threshold_and_conflict(self):
        steps=("Inspect.","Patch.","Verify.")
        for i in range(2):
            self.store.record_skill_evidence("debug.python","Debug Python",steps,
                evidence_id=f"s:{i}",succeeded=True,confidence=.9,source_kind="runtime")
        self.assertEqual(self.store.consolidate_skill("debug.python").status,"pending")
        self.store.record_skill_evidence("debug.python","Debug Python",steps,
            evidence_id="s:2",succeeded=True,confidence=.92,source_kind="runtime")
        d=self.store.consolidate_skill("debug.python")
        self.assertEqual(d.status,"consolidated")
        self.assertFalse(d.to_dict()["promotion_authorized"])

        self.store.record_skill_evidence("build.safe","Safe build",("Run tests.",),
            evidence_id="c:a",succeeded=True,confidence=.9,source_kind="runtime")
        self.store.record_skill_evidence("build.safe","Safe build",("Skip tests.",),
            evidence_id="c:b",succeeded=True,confidence=.9,source_kind="runtime")
        self.assertEqual(self.store.consolidate_skill("build.safe").status,"blocked")

if __name__=="__main__": unittest.main()
