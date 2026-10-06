from pathlib import Path
import sys, tempfile, unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.models import utc_now
from sira.papers import Paper, PaperBatch


class Provider:
    name="fake"; cache_namespace="fake-k"
    def search(self, query, max_results):
        rows = (
            Paper("p1","Software refactoring maintainability","Refactoring improves maintainability.",("R",),2026,"10.1/a","https://one.paper/a",None,utc_now(),"fake"),
            Paper("p2","Complex function refactoring","Complex functions can be decomposed while preserving behavior.",("R",),2026,"10.1/b","https://two.paper/b",None,utc_now(),"fake"),
        )
        return PaperBatch(rows[:max_results], api_requests=0, providers_attempted=(self.name,))


class OpportunityKnowledgeReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        for rel in ("src/sira","tests","benchmarks","docs"): (self.root/rel).mkdir(parents=True,exist_ok=True)
        (self.root/"sira.py").write_text("print('fixture')\n")
        (self.root/"README.md").write_text("fixture\n")
        (self.root/".env.example").write_text("\n")
        store=KnowledgeConsolidationStore(self.root)
        claim="Software engineering refactoring complex function maintainability behavior preservation."
        for eid,url,sha in (("k:1","https://one.example/a","a"*64),("k:2","https://two.example/a","b"*64)):
            store.record_evidence("code.refactor.complex",claim,evidence_id=eid,source_id=eid,source_url=url,confidence=.9,verifier_kind="fixture",evidence_sha256=sha,retrieved_at=utc_now(),verified=True)
        store.consolidate("code.refactor.complex")

    def tearDown(self): self.tmp.cleanup()

    def evidence(self):
        from sira.opportunity import discover_opportunities
        from sira.opportunity_evidence import build_opportunity_evidence
        lines=["def paper_read_run(value):"]
        for i in range(20): lines += [f"    if value == {i}:",f"        value += {i+1}"]
        lines += ["    return value",""]
        (self.root/"src/sira/paper_reading.py").write_text("\n".join(lines))
        (self.root/"tests/test_paper_reading.py").write_text("from sira.paper_reading import paper_read_run\n\ndef test_contract():\n    assert callable(paper_read_run)\n")
        opp=discover_opportunities(self.root,limit=20,now_epoch=2_000_000_000)["opportunities"][0]
        return build_opportunity_evidence(self.root,opp["opportunity_id"])

    def test_advisory_context_does_not_replace_writer_gate(self):
        from sira.opportunity_research import research_opportunity_free
        ev=self.evidence()
        report=research_opportunity_free(self.root,ev["evidence_id"],providers=(Provider(),),use_cache=False)
        self.assertEqual(report["local_knowledge_context"]["status"],"reused")
        self.assertTrue(any(row.get("basis")=="verified_local_knowledge_advisory" for row in report["candidate_strategies"]))
        self.assertEqual(report["research_quality"]["decision"],"writer_ready")
        self.assertFalse(report["local_knowledge_context"]["promotion_authorized"])


if __name__ == "__main__":
    unittest.main()
