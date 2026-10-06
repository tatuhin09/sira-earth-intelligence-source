from pathlib import Path
import tempfile
from .knowledge_mesh import search_knowledge_free, search_open_access_free
from .papers import Paper, PaperBatch
from .provider_catalog import get_provider
from .providers.wikimedia import KnowledgeBatch, KnowledgeRecord
from .research_mesh import research_mesh_plan
from .storage import RunStore, write_json

class FK:
    name="wikimedia"; cache_namespace="fake-wiki"
    def __init__(self): self.calls=0
    def search(self,q,m):
        self.calls+=1
        return KnowledgeBatch((KnowledgeRecord(
            "wikimedia:en:1","Python","Programming language",
            "https://en.wikipedia.org/wiki/Python","en",
            "2026-09-21T00:00:00+00:00"),),0,0,(self.name,))

class FD:
    name="doaj"; cache_namespace="fake-doaj"
    def __init__(self): self.calls=0
    def search(self,q,m):
        self.calls+=1
        return PaperBatch((Paper(
            "doaj:abc12345","Testing","Evidence",("R",),2026,"10.1/x",
            "https://doaj.org/article/abc12345",None,
            "2026-09-21T00:00:00+00:00","doaj",
            providers=("doaj",)),),0,providers_attempted=(self.name,))

def knowledge_source_benchmark(root: Path):
    root=Path(root).resolve(); cases=[]
    def add(i,v): cases.append({"case_id":i,"passed":bool(v)})
    with tempfile.TemporaryDirectory() as tmp:
        r=Path(tmp)
        w=get_provider("wikimedia"); d=get_provider("doaj")
        add("wiki_free",w.cost_class=="free" and w.access=="public")
        add("doaj_free",d.cost_class=="free" and d.access=="public")
        add("wiki_plan",research_mesh_plan(r,"knowledge",environ={})["selected_provider_ids"]==["wikimedia"])
        add("doaj_plan",research_mesh_plan(r,"open_access",environ={})["selected_provider_ids"]==["doaj"])
        fk=FK(); a=search_knowledge_free(r,"Please research Python",providers=(fk,))
        n=fk.calls; b=search_knowledge_free(r,"Please research Python",providers=(fk,))
        add("wiki_result",len(a["results"])==1)
        add("wiki_cache",fk.calls==n and b["metrics"]["cache_hit"])
        fd=FD(); c=search_open_access_free(r,"Please research testing",providers=(fd,))
        n=fd.calls; e=search_open_access_free(r,"Please research testing",providers=(fd,))
        add("doaj_result",len(c["results"])==1)
        add("doaj_cache",fd.calls==n and e["metrics"]["cache_hit"])
        add("no_spend",not a["paid_spending"] and not c["paid_spending"])
        add("no_metered",a["metered_provider_requests"]==0 and c["metered_provider_requests"]==0)
        add("no_authority",not a["authority_granted"] and not c["authority_granted"])
        add("no_promotion",not a["promotion_authorized"] and not c["promotion_authorized"])
    report={"schema_version":1,"kind":"knowledge_source_benchmark",
            "suite_id":"sira-knowledge-sources-v1.7d-a",
            "passed":sum(x["passed"] for x in cases),
            "failed":sum(not x["passed"] for x in cases),
            "api_requests":0,"cases":cases}
    run=RunStore(root); path=run.path/"knowledge-source-benchmark.json"
    write_json(path,report); return path,report
