import hashlib,json
from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]; SRC=ROOT/"src"
sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.storage import write_json
from sira.synthesis import ModelBatch, answer_run

class FixtureModel:
    name="fixture_synthesis"; model_id="fixture-v1"; cache_namespace="fixture-knowledge-v1"
    def __init__(self): self.calls=0
    def generate(self,task,payload,schema):
        self.calls+=1
        if task=="propose":
            data={"answer_language":"en","claims":[{"id":"C1","text":"Virtual environments isolate project package installations.","support_passage_ids":["E1","E2"],"opposing_passage_ids":[]}]}
        else:
            data={"claims":[{"id":"C1","decision":"supported","support_passage_ids":["E1","E2"],"opposing_passage_ids":[],"reason":"Both passages support the claim."}]}
        return ModelBatch(data,api_requests=1,input_tokens=5,output_tokens=3)

class SynthesisKnowledgeConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.parent_id="a"*32; parent_path=self.root/"runs"/self.parent_id/"result.json"; parent_path.parent.mkdir(parents=True)
        parent={"schema_version":1,"run_id":self.parent_id,"question":"What do virtual environments isolate?","sources":[]}
        parent_path.write_text(json.dumps(parent),encoding="utf-8")
        self.evidence_id="b"*32; run=self.root/"runs"/self.evidence_id; (run/"documents").mkdir(parents=True)
        docs=[]
        rows=(("S1","https://docs.python.org/3/library/venv.html","Python documentation","Virtual environments isolate project package installations."),
              ("S2","https://packaging.python.org/tutorials/installing-packages/","Python Packaging User Guide","Virtual environments isolate project package installations for a project."))
        for sid,url,title,text in rows:
            rel=Path("documents")/f"{sid}.txt"; (run/rel).write_text(text,encoding="utf-8")
            docs.append({"source_id":sid,"url":url,"title":title,"status":"read","trust":"untrusted","verification":"provider_text_retrieved","text_path":rel.as_posix(),"content_sha256":hashlib.sha256(text.encode()).hexdigest()})
        evidence={"schema_version":1,"kind":"source_evidence","run_id":self.evidence_id,"parent_run_id":self.parent_id,
                  "parent_sha256":hashlib.sha256(parent_path.read_bytes()).hexdigest(),"question":"What do virtual environments isolate?","status":"completed","documents":docs,"evidence":[],"metrics":{}}
        write_json(run/"evidence.json",evidence)
    def tearDown(self): self.tmp.cleanup()
    def test_two_source_accepted_claim_consolidates(self):
        model=FixtureModel(); path=answer_run(self.root,self.evidence_id,model,use_cache=True); result=json.loads((path/"answer.json").read_text())
        learning=result["knowledge_consolidation"]
        self.assertEqual(learning["status"],"completed"); self.assertEqual(learning["evidence_recorded"],2); self.assertEqual(learning["consolidated_claims"],1)
        stats=KnowledgeConsolidationStore(self.root).stats(); self.assertEqual(stats["active_knowledge"],1); self.assertFalse(stats["authority_granted"])
    def test_cached_answer_does_not_forge_second_evidence(self):
        model=FixtureModel(); answer_run(self.root,self.evidence_id,model,use_cache=True); before=KnowledgeConsolidationStore(self.root).stats()
        second=answer_run(self.root,self.evidence_id,model,use_cache=True); result=json.loads((second/"answer.json").read_text()); after=KnowledgeConsolidationStore(self.root).stats()
        self.assertEqual(model.calls,2); self.assertEqual(result["knowledge_consolidation"]["status"],"cache_replay_skipped"); self.assertEqual(before["knowledge_evidence"],after["knowledge_evidence"])
if __name__=="__main__": unittest.main()
