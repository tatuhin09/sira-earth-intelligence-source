"""Offline deterministic benchmark for SIRA's local learning-memory substrate."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile

from .memory import MemoryStore, Observation, learn_run, memory_quality_score
from .storage import RunStore, code_digest, write_json


DATASET = Path(__file__).resolve().parents[2] / "benchmarks" / "memory_cases.json"


def _write_run(root: Path, run_id: str, artifact: str, data: dict) -> None:
    path = root / "runs" / run_id
    path.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["run_id"] = run_id
    (path / artifact).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def memory_benchmark(root: Path):
    root = Path(root).resolve()
    dataset_raw = DATASET.read_bytes()
    dataset = json.loads(dataset_raw)
    cases = dataset.get("cases")
    if dataset.get("schema_version") != 1 or not isinstance(cases, list) or len(cases) != 24:
        raise ValueError("Invalid memory benchmark dataset")

    results = []
    with tempfile.TemporaryDirectory(prefix="sira-memory-benchmark-") as tmp:
        sandbox = Path(tmp)
        (sandbox / "runs").mkdir()
        store = MemoryStore(sandbox)

        # Case 1: repeated equivalent provider failure deduplicates, occurrences remain distinct.
        for run_id in ("a1" * 16, "a2" * 16):
            _write_run(sandbox, run_id, "papers.json", {
                "schema_version": 1, "status": "failed", "provider": "semantic_scholar",
                "error": {"code": "rate_limited"}, "metrics": {"failures": 1}, "papers": [],
            })
            learn_run(sandbox, run_id, store)
        rate_rows = store.search("rate limit")
        passed = len(rate_rows) == 1 and rate_rows[0]["occurrence_count"] == 2
        results.append({"case_id": "deduplicate_rate_limit", "passed": passed})

        # Case 2: successful run becomes validated reusable outcome memory.
        success_id = "b1" * 16
        _write_run(sandbox, success_id, "result.json", {
            "schema_version": 1, "status": "completed", "provider": "fixture",
            "error": None, "metrics": {"failures": 0}, "sources": [{"title": "ok"}],
        })
        learn_run(sandbox, success_id, store)
        success_rows = store.search("retrieval completed")
        passed = len(success_rows) == 1 and success_rows[0]["status"] == "validated"
        results.append({"case_id": "validated_success", "passed": passed})

        # Case 3: verifier rejection is retained as a searchable observed lesson.
        rejection_id = "c1" * 16
        _write_run(sandbox, rejection_id, "answer.json", {
            "schema_version": 1, "kind": "verified_evidence_answer", "status": "insufficient_evidence",
            "error": None, "model": {"provider": "fixture_model"}, "accepted_claims": [],
            "rejected_claims": [{"id": "C1", "text": "Unsupported benchmark claim",
                                  "reasons": ["unknown_citation"]}],
            "metrics": {"accepted_claims": 0, "rejected_claims": 1},
        })
        learn_run(sandbox, rejection_id, store)
        rejection_rows = store.search("Unsupported benchmark claim")
        passed = (len(rejection_rows) == 1 and rejection_rows[0]["kind"] == "rejection"
                  and rejection_rows[0]["category"] == "citation")
        results.append({"case_id": "rejected_claim", "passed": passed})

        # Case 4: old failure can be recovered by semantic-ish token search before new work starts.
        prior = store.search("semantic scholar rate limit")
        passed = len(prior) == 1 and prior[0]["capability"] == "scholarly_research"
        results.append({"case_id": "search_prior_failure", "passed": passed})

        # Case 5: validated memory outranks an equally recent observation.
        validated_quality = memory_quality_score(
            status="validated", occurrence_count=2, real_occurrence_count=2, age_days=5)
        observed_quality = memory_quality_score(
            status="observed", occurrence_count=2, real_occurrence_count=2, age_days=5)
        results.append({"case_id": "validated_outweighs_observed",
                        "passed": validated_quality > observed_quality})

        # Case 6: freshness breaks ties between equally reliable memories.
        fresh_quality = memory_quality_score(
            status="validated", occurrence_count=2, real_occurrence_count=2, age_days=5)
        stale_quality = memory_quality_score(
            status="validated", occurrence_count=2, real_occurrence_count=2, age_days=500)
        results.append({"case_id": "fresh_outweighs_stale", "passed": fresh_quality > stale_quality})

        # Case 7: contradictions reduce quality deterministically.
        clean_quality = memory_quality_score(
            status="validated", occurrence_count=2, real_occurrence_count=2,
            contradiction_count=0, age_days=5)
        contradicted_quality = memory_quality_score(
            status="validated", occurrence_count=2, real_occurrence_count=2,
            contradiction_count=2, age_days=5)
        results.append({"case_id": "contradiction_penalty",
                        "passed": clean_quality > contradicted_quality})

        # Case 8: synthetic fixture memory is auditable but cannot authorize autonomous work.
        fixture_id = "d1" * 16
        _write_run(sandbox, fixture_id, "result.json", {
            "schema_version": 1, "status": "failed", "provider": "fixture",
            "error": {"code": "timeout"}, "metrics": {"failures": 1}, "sources": [],
        })
        learn_run(sandbox, fixture_id, store)
        fixture_row = store.search("fixture timeout")[0]
        candidate_ids = {row["memory_id"] for row in store.improvement_candidates(20)}
        passed = (fixture_row["real_occurrence_count"] == 0
                  and fixture_row["synthetic_occurrence_count"] == 1
                  and fixture_row["memory_id"] not in candidate_ids)
        results.append({"case_id": "synthetic_excluded_from_autonomous", "passed": passed})

        # Case 9: supersession preserves the old lesson and links its replacement.
        old_id, _ = store.upsert(
            Observation("failure", "network", "retrieval", "old transport lesson", "observed",
                        provider="tavily", error_code="transport_error", signature="bench-old"),
            run_id="e1" * 16, artifact_name="result.json", artifact_sha256="1" * 64,
            outcome_status="failed",
        )
        new_id, _ = store.upsert(
            Observation("success", "success", "retrieval", "new transport lesson", "validated",
                        provider="tavily", signature="bench-new"),
            run_id="e2" * 16, artifact_name="result.json", artifact_sha256="2" * 64,
            outcome_status="completed",
        )
        store.supersede(old_id, new_id, "newer validated lesson", "e2" * 16)
        old, new = store.show(old_id), store.show(new_id)
        passed = (old["status"] == "superseded"
                  and old["superseded_by_memory_id"] == new_id
                  and new["supersedes_memory_id"] == old_id
                  and new["lineage_version"] >= 2)
        results.append({"case_id": "supersession_lineage", "passed": passed})

        # Case 10: protected owner/user preference authority stays outside machine memory.
        protected_blocked = False
        try:
            Observation("success", "success", "retrieval", "protected preference", "validated",
                        provider="owner", origin="owner_preference")
        except ValueError:
            protected_blocked = True
        results.append({"case_id": "protected_origin_separation", "passed": protected_blocked})

        cluster_a, _ = store.upsert(
            Observation("failure", "rate_limit", "scholarly_research", "Semantic Scholar rate limited request", "observed",
                        provider="semantic_scholar", error_code="rate_limited", signature="bench-cluster-a"),
            run_id="f1" * 16, artifact_name="papers.json", artifact_sha256="3" * 64, outcome_status="failed",
        )
        cluster_b, _ = store.upsert(
            Observation("failure", "rate_limit", "scholarly_research", "Semantic Scholar returned HTTP 429", "observed",
                        provider="semantic_scholar", error_code="http_429", signature="bench-cluster-b"),
            run_id="f2" * 16, artifact_name="papers.json", artifact_sha256="4" * 64, outcome_status="failed",
        )
        cluster = store.cluster(cluster_a)
        results.append({"case_id": "near_duplicate_cluster", "passed": cluster_a != cluster_b and {row["memory_id"] for row in cluster["members"]} >= {cluster_a, cluster_b}})

        unrelated, _ = store.upsert(
            Observation("failure", "network", "retrieval", "Tavily transport timeout", "observed",
                        provider="tavily", error_code="timeout", signature="bench-unrelated"),
            run_id="f3" * 16, artifact_name="result.json", artifact_sha256="5" * 64, outcome_status="failed",
        )
        results.append({"case_id": "unrelated_not_clustered", "passed": store.cluster(cluster_a)["cluster_key"] != store.cluster(unrelated)["cluster_key"]})

        conflict_fail, _ = store.upsert(
            Observation("failure", "provider", "retrieval", "Tavily same request pattern failed", "observed",
                        provider="tavily", error_code="provider_error", signature="bench-conflict-fail"),
            run_id="f4" * 16, artifact_name="result.json", artifact_sha256="6" * 64, outcome_status="failed",
        )
        conflict_success, _ = store.upsert(
            Observation("success", "success", "retrieval", "Tavily same request pattern completed successfully", "validated",
                        provider="tavily", signature="bench-conflict-success"),
            run_id="f5" * 16, artifact_name="result.json", artifact_sha256="7" * 64, outcome_status="completed",
        )
        proposals = store.reconcile_candidate(conflict_success)
        contradiction = next((row for row in proposals if row["other_memory_id"] == conflict_fail and row["relation_type"] == "contradicts"), None)
        if contradiction is not None:
            store.relate(conflict_fail, conflict_success, "contradicts", contradiction["confidence"], contradiction["reason"], evidence=contradiction.get("evidence"))
        results.append({"case_id": "real_contradiction", "passed": contradiction is not None and store.show(conflict_fail)["contradiction_count"] == 1 and store.show(conflict_success)["contradiction_count"] == 1})

        relation_first = store.relate(cluster_a, cluster_b, "similar", 0.8, "same bounded benchmark context")
        relation_second = store.relate(cluster_b, cluster_a, "similar", 0.8, "same bounded benchmark context")
        results.append({"case_id": "symmetric_relation_dedup", "passed": relation_first["relation_id"] == relation_second["relation_id"]})

        # A base cluster plus two relation hops must form one component.
        # An unrelated context must remain separate after the rebuild.
        bridge_b, _ = store.upsert(
            Observation("success", "success", "engineering", "A linked engineering lesson", "validated",
                        provider="openalex", signature="bench-transitive-bridge-b"),
            run_id="f7" * 16, artifact_name="result.json", artifact_sha256="b" * 64,
            outcome_status="completed",
        )
        bridge_c, _ = store.upsert(
            Observation("failure", "network", "knowledge", "A linked knowledge failure", "observed",
                        provider="crossref", signature="bench-transitive-bridge-c"),
            run_id="f8" * 16, artifact_name="result.json", artifact_sha256="c" * 64,
            outcome_status="failed",
        )
        store.relate(cluster_a, bridge_b, "similar", 0.8, "first transitive hop")
        store.relate(bridge_b, bridge_c, "similar", 0.8, "second transitive hop")
        clustered = [store.cluster(memory_id) for memory_id in (
            cluster_a, cluster_b, bridge_b, bridge_c,
        )]
        members = {row["memory_id"] for row in clustered[0]["members"]}
        passed = (
            all(item["cluster_key"] == clustered[0]["cluster_key"] for item in clustered)
            and all(item["cluster_size"] == clustered[0]["cluster_size"] for item in clustered)
            and {cluster_a, cluster_b, bridge_b, bridge_c} <= members
            and clustered[0]["cluster_size"] == len(members)
            and store.cluster(unrelated)["cluster_key"] != clustered[0]["cluster_key"]
        )
        results.append({"case_id": "transitive_cluster_chain", "passed": passed})

        synthetic_id, _ = store.upsert(
            Observation("failure", "provider", "retrieval", "fixture opposing retrieval result", "observed",
                        provider="fixture", error_code="provider_error", signature="bench-synthetic-conflict",
                        origin="synthetic_fixture", synthetic=True),
            run_id="f6" * 16, artifact_name="result.json", artifact_sha256="8" * 64, outcome_status="failed",
        )
        before_real = store.show(conflict_success)
        synthetic_blocked = False
        try:
            store.relate(conflict_success, synthetic_id, "contradicts", 0.9, "synthetic must not alter real confidence")
        except ValueError:
            synthetic_blocked = True
        after_real = store.show(conflict_success)
        results.append({"case_id": "synthetic_cannot_contaminate", "passed": synthetic_blocked and before_real["contradiction_count"] == after_real["contradiction_count"] and before_real["confidence"] == after_real["confidence"]})

        statuses = (store.show(conflict_fail)["status"], store.show(conflict_success)["status"])
        results.append({"case_id": "contradiction_does_not_supersede", "passed": "superseded" not in statuses})


        # Case 17: outcome events round-trip with bounded provenance.
        feedback_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "feedback retrieval lesson", "validated",
                provider="tavily", signature="bench-b3-feedback",
            ),
            run_id="71" * 16, artifact_name="result.json",
            artifact_sha256="1" * 64, outcome_status="completed",
        )
        feedback = store.record_outcome(
            feedback_id, "retrieval_used",
            context={"consumer": "memory_benchmark"}, weight=1.0,
        )
        results.append({
            "case_id": "outcome_roundtrip",
            "passed": feedback["outcome_type"] == "retrieval_used"
                      and store.outcomes(feedback_id)[0]["outcome_id"] == feedback["outcome_id"],
        })

        # Case 18: retrieval returns a bounded score and explicit reasons.
        explained = store.retrieve("feedback retrieval lesson", 10)
        explained_row = next(
            (row for row in explained if row["memory_id"] == feedback_id),
            None,
        )
        results.append({
            "case_id": "retrieval_score_explained",
            "passed": explained_row is not None
                      and 0.0 <= explained_row["retrieval_score"] <= 1.0
                      and any(
                          reason["component"] == "relevance"
                          for reason in explained_row["score_reasons"]
                      )
                      and any(
                          reason["component"] == "confidence"
                          for reason in explained_row["score_reasons"]
                      )
                      and any(
                          reason["component"] == "freshness"
                          for reason in explained_row["score_reasons"]
                      ),
        })

        # Case 19: successful reuse feedback raises ranking.
        help_a, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "benchmark ranking strategy alpha", "validated",
                provider="tavily", signature="bench-b3-help-a",
            ),
            run_id="72" * 16, artifact_name="result.json",
            artifact_sha256="2" * 64, outcome_status="completed",
        )
        help_b, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "benchmark ranking strategy beta", "validated",
                provider="tavily", signature="bench-b3-help-b",
            ),
            run_id="73" * 16, artifact_name="result.json",
            artifact_sha256="3" * 64, outcome_status="completed",
        )
        store.record_outcome(
            help_b, "retrieval_helped",
            context={"consumer": "memory_benchmark"}, weight=1.0,
        )
        help_rows = store.retrieve("benchmark ranking strategy", 20)
        help_scores = {
            row["memory_id"]: row["retrieval_score"] for row in help_rows
        }
        results.append({
            "case_id": "helped_feedback_boost",
            "passed": help_scores[help_b] > help_scores[help_a],
        })

        # Case 20: irrelevant feedback lowers ranking.
        irrelevant_a, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "benchmark routing lesson alpha", "validated",
                provider="tavily", signature="bench-b3-irrelevant-a",
            ),
            run_id="74" * 16, artifact_name="result.json",
            artifact_sha256="4" * 64, outcome_status="completed",
        )
        irrelevant_b, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "benchmark routing lesson beta", "validated",
                provider="tavily", signature="bench-b3-irrelevant-b",
            ),
            run_id="75" * 16, artifact_name="result.json",
            artifact_sha256="5" * 64, outcome_status="completed",
        )
        store.record_outcome(
            irrelevant_b, "retrieval_irrelevant",
            context={"consumer": "memory_benchmark"}, weight=1.0,
        )
        irrelevant_rows = store.retrieve("benchmark routing lesson", 20)
        irrelevant_scores = {
            row["memory_id"]: row["retrieval_score"]
            for row in irrelevant_rows
        }
        results.append({
            "case_id": "irrelevant_feedback_penalty",
            "passed": irrelevant_scores[irrelevant_a] > irrelevant_scores[irrelevant_b],
        })

        # Case 21: stale memory decays but remains retrievable and stored.
        stale_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "durable stale benchmark lesson", "validated",
                provider="tavily", signature="bench-b3-stale",
            ),
            run_id="76" * 16, artifact_name="result.json",
            artifact_sha256="6" * 64, outcome_status="completed",
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            conn.execute(
                "UPDATE memories SET last_seen_at=? WHERE memory_id=?",
                ("2020-01-01T00:00:00+00:00", stale_id),
            )
            conn.commit()
        stale_rows = store.retrieve("durable stale benchmark lesson", 20)
        stale_row = next(
            (row for row in stale_rows if row["memory_id"] == stale_id),
            None,
        )
        results.append({
            "case_id": "stale_memory_survives",
            "passed": stale_row is not None
                      and store.show(stale_id)["memory_id"] == stale_id
                      and any(
                          reason["component"] == "freshness"
                          and reason["effect"] == "penalty"
                          for reason in stale_row["score_reasons"]
                      ),
        })

        # Case 22: autonomous retrieval excludes synthetic-only memories.
        auto_real, _ = store.upsert(
            Observation(
                "failure", "network", "retrieval",
                "real autonomous benchmark timeout", "observed",
                provider="tavily", error_code="timeout",
                signature="bench-b3-auto-real",
            ),
            run_id="77" * 16, artifact_name="result.json",
            artifact_sha256="7" * 64, outcome_status="failed",
        )
        auto_synthetic, _ = store.upsert(
            Observation(
                "failure", "network", "retrieval",
                "synthetic autonomous benchmark timeout", "observed",
                provider="fixture", error_code="timeout",
                signature="bench-b3-auto-synthetic",
                origin="synthetic_fixture", synthetic=True,
            ),
            run_id="78" * 16, artifact_name="result.json",
            artifact_sha256="8" * 64, outcome_status="failed",
        )
        auto_ids = {
            row["memory_id"]
            for row in store.retrieve(
                "autonomous benchmark timeout", 20, autonomous=True
            )
        }
        results.append({
            "case_id": "autonomous_synthetic_excluded",
            "passed": auto_real in auto_ids and auto_synthetic not in auto_ids,
        })

        # Case 23: contradiction penalty is surfaced in score reasons.
        penalty_fail, _ = store.upsert(
            Observation(
                "failure", "provider", "retrieval",
                "penalty request pattern failed", "observed",
                provider="tavily", error_code="provider_error",
                signature="bench-b3-penalty-fail",
            ),
            run_id="79" * 16, artifact_name="result.json",
            artifact_sha256="9" * 64, outcome_status="failed",
        )
        penalty_success, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "penalty request pattern succeeded", "validated",
                provider="tavily", signature="bench-b3-penalty-success",
            ),
            run_id="7a" * 16, artifact_name="result.json",
            artifact_sha256="a" * 64, outcome_status="completed",
        )
        store.relate(
            penalty_fail, penalty_success, "contradicts", 0.9,
            "same provider/context opposite outcome",
        )
        penalty_rows = store.retrieve("penalty request pattern", 20)
        penalty_row = next(
            row for row in penalty_rows
            if row["memory_id"] == penalty_success
        )
        results.append({
            "case_id": "retrieval_contradiction_penalty",
            "passed": any(
                reason["component"] == "contradiction"
                and reason["effect"] == "penalty"
                for reason in penalty_row["score_reasons"]
            ),
        })

        # Case 24: feedback does not change legacy search semantics/order.
        search_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "legacy search compatibility lesson", "validated",
                provider="tavily", signature="bench-b3-search",
            ),
            run_id="7b" * 16, artifact_name="result.json",
            artifact_sha256="b" * 64, outcome_status="completed",
        )
        search_before = [
            row["memory_id"]
            for row in store.search("legacy search compatibility", 20)
        ]
        store.record_outcome(
            search_id, "retrieval_irrelevant",
            context={"consumer": "memory_benchmark"}, weight=1.0,
        )
        search_after = [
            row["memory_id"]
            for row in store.search("legacy search compatibility", 20)
        ]
        results.append({
            "case_id": "search_backward_compatible",
            "passed": search_before == search_after,
        })


    passed_count = sum(bool(case["passed"]) for case in results)
    report = {
        "schema_version": 1,
        "suite_id": dataset["suite_id"],
        "kind": "synthetic_memory_regression_not_live_learning_quality",
        "dataset_sha256": hashlib.sha256(dataset_raw).hexdigest(),
        "code_sha256": code_digest(),
        "passed": passed_count,
        "failed": len(results) - passed_count,
        "api_requests": 0,
        "cases": results,
        "limitations": [
            "Fixtures validate deterministic persistence, provenance hygiene, lineage, and quality-order invariants.",
            "The benchmark does not prove semantic truth or causal correctness of a learned memory.",
            "No model or network provider is invoked by this benchmark.",
        ],
    }
    run = RunStore(root)
    path = run.path / "memory-benchmark.json"
    write_json(path, report)
    run.event("benchmark_finished", suite=dataset["suite_id"], passed=report["passed"], failed=report["failed"])
    return path, report
