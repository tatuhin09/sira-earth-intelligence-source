"""Offline benchmark for Strategy Learner v1."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile

from .memory import MemoryStore, Observation
from .strategy_learner import StrategyLearner
from .storage import RunStore, code_digest, write_json


DATASET = Path(__file__).resolve().parents[2] / "benchmarks" / "strategy_cases.json"


def _candidates():
    return [
        {"strategy_id": "semantic_scholar", "base_score": 0.70, "available": True, "policy_allowed": True, "metered": False},
        {"strategy_id": "crossref", "base_score": 0.70, "available": True, "policy_allowed": True, "metered": False},
        {"strategy_id": "tavily", "base_score": 0.72, "available": True, "policy_allowed": False, "metered": True},
    ]


def _memory(store, signature, provider="semantic_scholar"):
    memory_id, _ = store.upsert(
        Observation(
            "success", "success", "scholarly_research",
            f"{provider} strategy outcome", "validated",
            provider=provider, signature=signature,
        ),
        run_id=(signature[-2:] or "aa") * 16,
        artifact_name="result.json",
        artifact_sha256=(signature[0] if signature else "a") * 64,
        outcome_status="completed",
    )
    return memory_id


def strategy_benchmark(root: Path):
    root = Path(root).resolve()
    dataset_raw = DATASET.read_bytes()
    dataset = json.loads(dataset_raw)
    cases = dataset.get("cases")
    if dataset.get("schema_version") != 1 or not isinstance(cases, list) or len(cases) != 8:
        raise ValueError("Invalid strategy benchmark dataset")

    results = []
    with tempfile.TemporaryDirectory(prefix="sira-strategy-benchmark-") as tmp:
        sandbox = Path(tmp)
        (sandbox / "runs").mkdir()
        store = MemoryStore(sandbox)
        learner = StrategyLearner(sandbox)

        decision = learner.rank("scholarly_research", _candidates())
        results.append({
            "case_id": "deterministic_fallback",
            "passed": decision["recommended_strategy_id"] == "crossref"
                      and decision["used_learning"] is False,
        })

        success_id = _memory(store, "bench-success")
        for _ in range(2):
            store.record_outcome(
                success_id, "application_succeeded",
                context={"strategy_id": "semantic_scholar", "capability": "scholarly_research"},
                weight=1.0,
            )
        decision = learner.rank("scholarly_research", _candidates())
        semantic = next(x for x in decision["ranking"] if x["strategy_id"] == "semantic_scholar")
        results.append({
            "case_id": "positive_outcome_boost",
            "passed": decision["recommended_strategy_id"] == "semantic_scholar"
                      and semantic["learned_adjustment"] > 0,
        })

        with tempfile.TemporaryDirectory(prefix="sira-strategy-threshold-") as t2:
            r2 = Path(t2)
            (r2 / "runs").mkdir()
            s2 = MemoryStore(r2)
            m2 = _memory(s2, "bench-threshold")
            s2.record_outcome(
                m2, "application_succeeded",
                context={"strategy_id": "semantic_scholar", "capability": "scholarly_research"},
                weight=1.0,
            )
            d2 = StrategyLearner(r2).rank("scholarly_research", _candidates())
            row = next(x for x in d2["ranking"] if x["strategy_id"] == "semantic_scholar")
            results.append({
                "case_id": "minimum_evidence_threshold",
                "passed": row["learned_adjustment"] == 0.0 and d2["used_learning"] is False,
            })

        with tempfile.TemporaryDirectory(prefix="sira-strategy-negative-") as t3:
            r3 = Path(t3)
            (r3 / "runs").mkdir()
            s3 = MemoryStore(r3)
            m3 = _memory(s3, "bench-negative")
            for _ in range(3):
                s3.record_outcome(
                    m3, "application_failed",
                    context={"strategy_id": "semantic_scholar", "capability": "scholarly_research"},
                    weight=1.0,
                )
            d3 = StrategyLearner(r3).rank("scholarly_research", _candidates())
            row = next(x for x in d3["ranking"] if x["strategy_id"] == "semantic_scholar")
            results.append({
                "case_id": "negative_outcome_penalty",
                "passed": row["eligible"] and row["learned_adjustment"] < 0,
            })

        with tempfile.TemporaryDirectory(prefix="sira-strategy-synthetic-") as t4:
            r4 = Path(t4)
            (r4 / "runs").mkdir()
            s4 = MemoryStore(r4)
            m4, _ = s4.upsert(
                Observation(
                    "success", "success", "scholarly_research",
                    "fixture provider success", "validated",
                    provider="fixture", signature="bench-synth",
                    origin="synthetic_fixture", synthetic=True,
                ),
                run_id="ab" * 16, artifact_name="result.json",
                artifact_sha256="a" * 64, outcome_status="completed",
            )
            for _ in range(4):
                s4.record_outcome(
                    m4, "application_succeeded",
                    context={"strategy_id": "semantic_scholar", "capability": "scholarly_research"},
                    weight=1.0,
                )
            d4 = StrategyLearner(r4).rank("scholarly_research", _candidates())
            row = next(x for x in d4["ranking"] if x["strategy_id"] == "semantic_scholar")
            results.append({
                "case_id": "synthetic_isolation",
                "passed": row["evidence"]["eligible_events"] == 0 and row["learned_adjustment"] == 0.0,
            })

        with tempfile.TemporaryDirectory(prefix="sira-strategy-contradiction-") as t5:
            r5 = Path(t5)
            (r5 / "runs").mkdir()
            s5 = MemoryStore(r5)
            clean = _memory(s5, "bench-clean", "semantic_scholar")
            conflict = _memory(s5, "bench-conflict", "crossref")
            opposite, _ = s5.upsert(
                Observation(
                    "failure", "provider", "scholarly_research",
                    "Crossref same context failed", "observed",
                    provider="crossref", error_code="provider_error",
                    signature="bench-opposite",
                ),
                run_id="cd" * 16, artifact_name="result.json",
                artifact_sha256="c" * 64, outcome_status="failed",
            )
            s5.relate(conflict, opposite, "contradicts", 0.9, "same context opposite result")
            for mid, sid in ((clean, "semantic_scholar"), (conflict, "crossref")):
                for _ in range(2):
                    s5.record_outcome(
                        mid, "application_succeeded",
                        context={"strategy_id": sid, "capability": "scholarly_research"},
                        weight=1.0,
                    )
            d5 = StrategyLearner(r5).rank("scholarly_research", _candidates())
            rows = {x["strategy_id"]: x for x in d5["ranking"]}
            results.append({
                "case_id": "contradiction_discount",
                "passed": rows["semantic_scholar"]["learned_adjustment"] > rows["crossref"]["learned_adjustment"],
            })

        with tempfile.TemporaryDirectory(prefix="sira-strategy-policy-") as t6:
            r6 = Path(t6)
            (r6 / "runs").mkdir()
            s6 = MemoryStore(r6)
            m6 = _memory(s6, "bench-policy", "tavily")
            for _ in range(4):
                s6.record_outcome(
                    m6, "application_succeeded",
                    context={"strategy_id": "tavily", "capability": "scholarly_research"},
                    weight=1.0,
                )
            d6 = StrategyLearner(r6).rank("scholarly_research", _candidates())
            row = next(x for x in d6["ranking"] if x["strategy_id"] == "tavily")
            results.append({
                "case_id": "policy_veto",
                "passed": not row["eligible"] and d6["recommended_strategy_id"] != "tavily",
            })

        rendered = repr(decision)
        results.append({
            "case_id": "aggregate_only_output",
            "passed": "context_json" not in rendered
                      and "raw_error" not in rendered
                      and "token" not in rendered,
        })

    passed = sum(bool(x["passed"]) for x in results)
    report = {
        "schema_version": 1,
        "suite_id": dataset["suite_id"],
        "kind": "synthetic_strategy_learning_regression_not_live_policy_authority",
        "dataset_sha256": hashlib.sha256(dataset_raw).hexdigest(),
        "code_sha256": code_digest(),
        "passed": passed,
        "failed": len(results) - passed,
        "api_requests": 0,
        "cases": results,
        "limitations": [
            "The benchmark validates bounded ranking invariants, not causal truth.",
            "Policy and provider eligibility remain external to the learner.",
            "No network or model provider is invoked.",
        ],
    }
    run = RunStore(root)
    path = run.path / "strategy-benchmark.json"
    write_json(path, report)
    run.event("benchmark_finished", suite=dataset["suite_id"], passed=report["passed"], failed=report["failed"])
    return path, report
