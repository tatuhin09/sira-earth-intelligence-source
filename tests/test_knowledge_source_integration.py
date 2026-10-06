from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]; SRC=ROOT/"src"
sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.knowledge_mesh import search_knowledge_free,search_open_access_free
from sira.knowledge_source_benchmark import FK,FD
from sira.provider_catalog import get_provider
from sira.research_mesh import research_mesh_plan

class KnowledgeSourceIntegrationTests(unittest.TestCase):
    def test_distinct_capabilities(self):
        self.assertIn("knowledge_search",get_provider("wikimedia").capabilities)
        self.assertIn("open_access_search",get_provider("doaj").capabilities)
        self.assertNotIn("paper_search",get_provider("doaj").capabilities)
        with tempfile.TemporaryDirectory() as t:
            r=Path(t)
            self.assertEqual(research_mesh_plan(r,"knowledge",environ={})["selected_provider_ids"],["wikimedia"])
            self.assertEqual(research_mesh_plan(r,"open_access",environ={})["selected_provider_ids"],["doaj"])
    def test_cache_and_non_authority(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); fk=FK()
            a=search_knowledge_free(r,"Please research Python",providers=(fk,))
            n=fk.calls; b=search_knowledge_free(r,"Please research Python",providers=(fk,))
            self.assertEqual(fk.calls,n); self.assertTrue(b["metrics"]["cache_hit"])
            self.assertFalse(a["authority_granted"])
            fd=FD(); c=search_open_access_free(r,"Please research testing",providers=(fd,))
            self.assertEqual(c["results"][0]["provider"],"doaj")
            self.assertFalse(c["paid_spending"])
