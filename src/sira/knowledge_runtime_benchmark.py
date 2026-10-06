from pathlib import Path
import tempfile

from .knowledge_consolidation import KnowledgeConsolidationStore
from .knowledge_runtime import (
    KnowledgeRevalidationLedger,
    knowledge_context_for_query,
    knowledge_revalidation_targets,
    revalidate_knowledge_target,
    resolve_knowledge_query,
)
from .storage import RunStore, write_json


def _seed(store, key, claim, when):
    for eid, url, sha in (
        ("e:1", "https://one.example/a", "a" * 64),
        ("e:2", "https://two.example/a", "b" * 64),
    ):
        store.record_evidence(
            key, claim, evidence_id=eid, source_id=eid, source_url=url,
            confidence=.9, verifier_kind="benchmark", evidence_sha256=sha,
            retrieved_at=when, verified=True,
        )
    store.consolidate(key, stale_after_days=30)


def knowledge_runtime_benchmark(root: Path):
    root = Path(root).resolve()
    cases = []
    add = lambda name, passed: cases.append({"case_id": name, "passed": bool(passed)})
    with tempfile.TemporaryDirectory(prefix="sira-krt-") as tmp:
        sandbox = Path(tmp)
        store = KnowledgeConsolidationStore(sandbox)
        _seed(store, "fact.fresh", "Virtual environments isolate project package installations.", "2026-09-20T00:00:00+00:00")
        context = knowledge_context_for_query(sandbox, "virtual environments package installations", now="2026-09-21T00:00:00+00:00")
        add("fresh_local_reuse", context["result_count"] == 1)
        add("fresh_local_zero_api", context["api_requests"] == 0)
        resolved = resolve_knowledge_query(sandbox, "virtual environments package installations", allow_research_fallback=True, now="2026-09-21T00:00:00+00:00")
        add("fresh_skips_research", resolved["mode"] == "local_reuse" and resolved["research"] is None)

        _seed(store, "fact.stale", "A stale verified claim needs revalidation.", "2025-01-01T00:00:00+00:00")
        targets = knowledge_revalidation_targets(sandbox, now_epoch=1_790_000_000.0)
        stale = next(row for row in targets if row["knowledge_key"] == "fact.stale")
        add("stale_target", stale["target_kind"] == "knowledge_revalidation")
        add("before_proactive_opportunity", stale["priority_class"] == 2)

        fake_k = lambda *_a, **_k: {"results": [{"title": "Reference"}], "metrics": {"api_requests": 0}}
        fake_o = lambda *_a, **_k: {"results": [{"title": "Article"}], "metrics": {"api_requests": 0}}
        report = revalidate_knowledge_target(
            sandbox, stale, knowledge_searcher=fake_k, open_access_searcher=fake_o,
            now_epoch=1_790_000_000.0
        )
        add("discovery_only", report["outcome"] == "verification_refresh_required" and report["refresh_performed"] is False)
        add("synthesis_gate", report["verified_synthesis_required"] is True)
        add("never_promotes", not report["promotion_performed"] and not report["main_tree_modified"])
        state = KnowledgeRevalidationLedger(sandbox).state("fact.stale", now_epoch=1_790_000_001.0)
        add("cooldown", not state["eligible"] and state["remaining_seconds"] > 0)
        again = knowledge_revalidation_targets(sandbox, now_epoch=1_790_000_001.0)
        add("cooldown_filters", "fact.stale" not in {row["knowledge_key"] for row in again})
        add("authority_none", report["authority_granted"] is False and report["promotion_authorized"] is False and report["paid_spending"] is False)
        add("metered_none", report["metered_model_requests"] == 0)

    report = {
        "schema_version": 1,
        "kind": "knowledge_runtime_benchmark",
        "suite_id": "sira-knowledge-runtime-v1.7d-d",
        "passed": sum(row["passed"] for row in cases),
        "failed": sum(not row["passed"] for row in cases),
        "api_requests": 0,
        "cases": cases,
    }
    run = RunStore(root)
    path = run.path / "knowledge-runtime-benchmark.json"
    write_json(path, report)
    return path, report
