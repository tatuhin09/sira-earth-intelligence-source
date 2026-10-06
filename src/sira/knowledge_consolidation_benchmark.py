from __future__ import annotations
from pathlib import Path
import tempfile
from .knowledge_consolidation import KnowledgeConsolidationStore, record_verified_answer_claims
from .storage import RunStore, write_json


def knowledge_consolidation_benchmark(root: Path):
    root = Path(root).resolve()
    cases = []
    add = lambda cid, ok: cases.append({"case_id": cid, "passed": bool(ok)})
    with tempfile.TemporaryDirectory(prefix="sira-knowledge-v17d-b-") as tmp:
        sandbox = Path(tmp)
        store = KnowledgeConsolidationStore(sandbox)
        key = "python.virtualenv.packages"
        claim = "Virtual environments isolate project package installations."
        store.record_evidence(key, claim, evidence_id="ev:a", source_id="S1",
            source_url="https://docs.python.org/3/library/venv.html", confidence=.92,
            verifier_kind="benchmark", evidence_sha256="a"*64,
            retrieved_at="2026-09-21T00:00:00+00:00", verified=True)
        add("single_source_pending", store.consolidate(key).status == "pending")
        store.record_evidence(key, claim, evidence_id="ev:b", source_id="S2",
            source_url="https://packaging.python.org/tutorials/installing-packages/", confidence=.90,
            verifier_kind="benchmark", evidence_sha256="b"*64,
            retrieved_at="2026-09-21T00:00:00+00:00", verified=True)
        d = store.consolidate(key)
        add("two_hosts_consolidate", d.status == "consolidated" and d.host_count == 2)
        add("duplicate_idempotent", not store.record_evidence(key, claim, evidence_id="ev:a", source_id="S1",
            source_url="https://docs.python.org/3/library/venv.html", confidence=.92,
            verifier_kind="benchmark", evidence_sha256="a"*64,
            retrieved_at="2026-09-21T00:00:00+00:00", verified=True))
        before = store.stats()["knowledge_evidence"]
        cached = record_verified_answer_claims(sandbox, evidence_sha256="c"*64,
            accepted_claims=[{"text":"Cached claim","source_ids":["S1","S2"]}],
            sources=[{"source_id":"S1","url":"https://a.example/x"},{"source_id":"S2","url":"https://b.example/y"}],
            verified_at="2026-09-21T00:00:00+00:00", cache_replay=True)
        add("cache_replay_skipped", cached["status"] == "cache_replay_skipped" and before == store.stats()["knowledge_evidence"])
        key2 = "same.host.claim"
        for i, path in enumerate(("a","b"), 1):
            store.record_evidence(key2, "One host is not independent evidence.", evidence_id=f"sh:{i}", source_id=f"S{i}",
                source_url=f"https://same.example/{path}", confidence=.95, verifier_kind="benchmark",
                evidence_sha256=(str(i)*64), retrieved_at="2026-09-21T00:00:00+00:00", verified=True)
        add("same_host_pending", store.consolidate(key2).reason == "insufficient_source_independence")
        key3 = "conflict.demo"
        for eid, text, url, sha in (
            ("ca:1","The setting is enabled.","https://one.example/a","d"*64),
            ("ca:2","The setting is enabled.","https://two.example/a","e"*64)):
            store.record_evidence(key3, text, evidence_id=eid, source_id=eid, source_url=url, confidence=.9,
                verifier_kind="benchmark", evidence_sha256=sha, retrieved_at="2026-09-21T00:00:00+00:00", verified=True)
        add("pre_conflict_active", store.consolidate(key3).status == "consolidated")
        for eid, url, sha in (("cb:1","https://three.example/b","f"*64),("cb:2","https://four.example/b","1"*64)):
            store.record_evidence(key3, "The setting is disabled.", evidence_id=eid, source_id=eid, source_url=url, confidence=.9,
                verifier_kind="benchmark", evidence_sha256=sha, retrieved_at="2026-09-21T00:00:00+00:00", verified=True)
        add("conflict_blocked", store.consolidate(key3).status == "blocked")
        add("conflict_not_reused", store.search("setting enabled", now="2026-09-22T00:00:00+00:00") == [])
        fresh = store.lookup(key, now="2026-09-22T00:00:00+00:00")
        add("fresh_reusable", fresh is not None and fresh["freshness"] == "fresh")
        stale = store.lookup(key, now="2027-09-22T00:00:00+00:00")
        add("stale_marked", stale is not None and stale["revalidation_required"] is True)
        add("stale_default_excluded", store.search("virtual environments package", now="2027-09-22T00:00:00+00:00") == [])
        add("stale_revalidation_candidate", key in {r["knowledge_key"] for r in store.revalidation_candidates(now="2027-09-22T00:00:00+00:00")})
        stats = store.stats()
        add("non_authoritative", not stats["authority_granted"] and not stats["promotion_authorized"] and not stats["paid_spending_authorized"])
    report = {"schema_version":1,"kind":"knowledge_consolidation_benchmark","suite_id":"sira-knowledge-consolidation-v1.7d-b",
              "passed":sum(r["passed"] for r in cases),"failed":sum(not r["passed"] for r in cases),"api_requests":0,"cases":cases}
    run = RunStore(root)
    path = run.path / "knowledge-consolidation-benchmark.json"
    write_json(path, report)
    return path, report
