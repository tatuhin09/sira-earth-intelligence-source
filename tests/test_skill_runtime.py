from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]; SRC=ROOT/"src"; sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.learning_consolidation import LearningConsolidationStore
from sira.skill_runtime import record_verified_handoff_skill,safe_record_verified_handoff_skill,skill_context_for_opportunity,skill_spec_for_opportunity

def success(handoff_id):
    return {"handoff_id":handoff_id,"status":"promoted","outcome":"promotion_committed","promotion_performed":True,
            "main_tree_modified":True,"structural_check":{"decision":"pass"}}

class SkillRuntimeTests(unittest.TestCase):
    def test_supported_opportunity_has_stable_bounded_skill(self):
        spec=skill_spec_for_opportunity({"type":"complex_function"}); self.assertEqual(spec["skill_key"],"code.refactor.complex_function"); self.assertEqual(len(spec["steps"]),4)
    def test_only_protected_success_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); row=record_verified_handoff_skill(root,{"type":"complex_function"},{"handoff_id":"ohf_"+"a"*32,"status":"rejected","outcome":"rejected_evaluator1","promotion_performed":False,"main_tree_modified":False,"structural_check":{"decision":"pass"}})
            self.assertEqual(row["status"],"not_recorded"); self.assertEqual(LearningConsolidationStore(root).stats()["skill_evidence"],0)
    def test_three_distinct_successes_consolidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); opp={"type":"complex_function"}
            for c in ("1","2","3"): row=record_verified_handoff_skill(root,opp,success("ohf_"+c*32))
            self.assertEqual(row["consolidation"]["status"],"consolidated"); context=skill_context_for_opportunity(root,opp)
            self.assertEqual(context["evidence_count"],3); self.assertTrue(context["advisory_only"]); self.assertFalse(context["promotion_authorized"])
    def test_duplicate_handoff_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); opp={"type":"complex_function"}; h=success("ohf_"+"b"*32)
            self.assertTrue(record_verified_handoff_skill(root,opp,h)["evidence_recorded"])
            second=record_verified_handoff_skill(root,opp,h); self.assertEqual(second["status"],"duplicate_evidence"); self.assertEqual(LearningConsolidationStore(root).stats()["skill_evidence"],1)
    def test_safe_wrapper_contains_learning_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            row=safe_record_verified_handoff_skill(Path(tmp),{"type":"complex_function"},{"handoff_id":"bad id","status":"promoted","outcome":"promotion_committed","promotion_performed":True,"main_tree_modified":True,"structural_check":{"decision":"pass"}})
        self.assertEqual(row["status"],"learning_error"); self.assertFalse(row["authority_granted"])
if __name__=="__main__": unittest.main()
