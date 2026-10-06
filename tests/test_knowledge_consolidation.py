from pathlib import Path
import sys, tempfile, unittest
ROOT=Path(__file__).resolve().parents[1]; SRC=ROOT/"src"
sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.knowledge_consolidation import KnowledgeConsolidationStore, record_verified_answer_claims

class KnowledgeConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.store=KnowledgeConsolidationStore(self.root)
    def tearDown(self): self.tmp.cleanup()
    def rec(self,key,claim,eid,url,sha,when="2026-09-21T00:00:00+00:00"):
        return self.store.record_evidence(key,claim,evidence_id=eid,source_id=eid,source_url=url,confidence=.9,
            verifier_kind="fixture",evidence_sha256=sha,retrieved_at=when,verified=True)
    def test_two_distinct_hosts_required(self):
        k="fact.python.venv"; c="Virtual environments isolate project package installations."
        self.rec(k,c,"e:a","https://docs.python.org/3/library/venv.html","a"*64)
        self.assertEqual(self.store.consolidate(k).reason,"insufficient_distinct_sources")
        self.rec(k,c,"e:b","https://packaging.python.org/tutorials/installing-packages/","b"*64)
        d=self.store.consolidate(k); self.assertEqual((d.status,d.host_count),("consolidated",2)); self.assertFalse(d.to_dict()["promotion_authorized"])
    def test_same_host_is_not_independent(self):
        k="fact.same.host"; c="Same-host duplication is not independent confirmation."
        self.rec(k,c,"e:a","https://example.org/a","a"*64); self.rec(k,c,"e:b","https://example.org/b","b"*64)
        self.assertEqual(self.store.consolidate(k).reason,"insufficient_source_independence")
    def test_duplicate_and_cache_replay_do_not_forge_evidence(self):
        k="fact.duplicate"; c="Duplicate evidence is idempotent."
        self.assertTrue(self.rec(k,c,"e:a","https://one.example/a","a"*64)); self.assertFalse(self.rec(k,c,"e:a","https://one.example/a","a"*64))
        before=self.store.stats()["knowledge_evidence"]
        result=record_verified_answer_claims(self.root,evidence_sha256="c"*64,
            accepted_claims=[{"text":"Cached result","source_ids":["S1","S2"]}],
            sources=[{"source_id":"S1","url":"https://one.example/x"},{"source_id":"S2","url":"https://two.example/x"}],
            verified_at="2026-09-21T00:00:00+00:00",cache_replay=True)
        self.assertEqual(result["status"],"cache_replay_skipped"); self.assertEqual(self.store.stats()["knowledge_evidence"],before)
    def test_conflict_fails_closed(self):
        k="fact.feature.state"
        self.rec(k,"The feature is enabled.","a:1","https://one.example/a","a"*64); self.rec(k,"The feature is enabled.","a:2","https://two.example/a","b"*64)
        self.assertEqual(self.store.consolidate(k).status,"consolidated")
        self.rec(k,"The feature is disabled.","b:1","https://three.example/b","c"*64); self.rec(k,"The feature is disabled.","b:2","https://four.example/b","d"*64)
        self.assertEqual(self.store.consolidate(k).status,"blocked")
        self.assertEqual(self.store.lookup(k,now="2026-09-22T00:00:00+00:00")["status"],"conflicted")
        self.assertEqual(self.store.search("feature enabled",now="2026-09-22T00:00:00+00:00"),[])
    def test_stale_retained_but_not_default_reuse(self):
        k="fact.stale.demo"; c="A previously verified fact remains auditable when stale."
        self.rec(k,c,"s:1","https://one.example/a","a"*64,"2025-01-01T00:00:00+00:00"); self.rec(k,c,"s:2","https://two.example/a","b"*64,"2025-01-01T00:00:00+00:00")
        self.store.consolidate(k,stale_after_days=30); shown=self.store.lookup(k,now="2026-09-21T00:00:00+00:00")
        self.assertEqual(shown["freshness"],"stale"); self.assertEqual(self.store.search("previously verified fact",now="2026-09-21T00:00:00+00:00"),[])
        self.assertIn(k,{r["knowledge_key"] for r in self.store.revalidation_candidates(now="2026-09-21T00:00:00+00:00")})
    def test_low_confidence_and_unverified_ignored(self):
        self.assertFalse(self.store.record_evidence("fact.low.confidence","Low confidence.",evidence_id="lc:1",source_id="S1",source_url="https://one.example/a",confidence=.2,verifier_kind="fixture",evidence_sha256="a"*64,retrieved_at="2026-09-21T00:00:00+00:00",verified=True))
        self.assertFalse(self.store.record_evidence("fact.unverified","Unverified.",evidence_id="uv:1",source_id="S1",source_url="https://one.example/a",confidence=.99,verifier_kind="fixture",evidence_sha256="b"*64,retrieved_at="2026-09-21T00:00:00+00:00",verified=False))
        self.assertEqual(self.store.stats()["knowledge_evidence"],0)
if __name__=="__main__": unittest.main()
