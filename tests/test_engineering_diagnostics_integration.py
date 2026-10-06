from pathlib import Path
import hashlib,sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/"src"))
from sira.engineering_verification import execute_verification_plan

def plan():
    return {"schema":"sira.engineering_verification_plan.v1","candidate_only_required":True,"network_disabled_required":True,
            "execution_performed":False,"commands":[{"command_id":"go.test","kind":"test","language":"go",
            "argv":["go","test","./..."],"cwd":".","env":{"GOPROXY":"off","GOSUMDB":"off"},"available":True,
            "candidate_only":True,"network_policy":"sandbox_network_must_be_disabled","writes_generated_artifacts":True,
            "shell":False,"authority_granted":False,"promotion_authorized":False}]}

class EngineeringDiagnosticsIntegrationTests(unittest.TestCase):
    def test_in_memory_text_becomes_structured_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); raw="./main.go:7:3: undefined: x token=super-secret"
            def runner(argv,**kwargs):
                return {"status":"failed","returncode":1,"timed_out":False,"output_limit_exceeded":False,
                        "output_bytes":len(raw),"output_sha256":hashlib.sha256(raw.encode()).hexdigest(),
                        "duration_ms":1,"diagnostic_text":raw}
            result=execute_verification_plan(root,plan(),command_runner=runner)
            self.assertEqual(result["diagnostic_count"],1)
            self.assertEqual(result["commands"][0]["diagnostics"][0]["path"],"main.go")
            self.assertNotIn("super-secret",repr(result))
            self.assertNotIn("diagnostic_text",repr(result))
            self.assertFalse(result["raw_output_included"])
