"""Offline deterministic benchmark for the v0.9 improvement-loop foundation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile

from .improvement import plan_improvement, research_memory, run_experiment
from .memory import MemoryStore, Observation
from .storage import RunStore, code_digest, write_json

DATASET = Path(__file__).resolve().parents[2] / "benchmarks" / "improvement_cases.json"


class _PassRunner:
    def evaluate(self, root: Path, suite: str | None) -> dict:
        return {"tests": {"passed": True, "test_count": 1, "returncode": 0},
                "benchmark": ({"passed": True, "passed_cases": 1, "failed_cases": 0, "returncode": 0}
                              if suite else None),
                "overall_passed": True}


def _seed_project(root: Path) -> None:
    (root / "runs").mkdir(parents=True, exist_ok=True)
    (root / "src" / "sira").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir()
    (root / "benchmarks").mkdir()
    (root / "docs").mkdir()
    (root / "sira.py").write_text("print('fixture')\n", encoding="utf-8")
    (root / "src" / "sira" / "fixture.py").write_text("X = 1\n", encoding="utf-8")
    (root / "tests" / "test_fixture.py").write_text("# fixture\n", encoding="utf-8")


def improvement_benchmark(root: Path):
    root = Path(root).resolve()
    dataset_raw = DATASET.read_bytes()
    dataset = json.loads(dataset_raw)
    cases = dataset.get("cases")
    if dataset.get("schema_version") != 1 or not isinstance(cases, list) or len(cases) != 4:
        raise ValueError("Invalid improvement benchmark dataset")

    results = []
    with tempfile.TemporaryDirectory(prefix="sira-improvement-benchmark-") as tmp:
        sandbox = Path(tmp)
        _seed_project(sandbox)
        store = MemoryStore(sandbox)
        repeated = Observation("failure", "rate_limit", "scholarly_research", "Repeated rate limit", "observed",
                               provider="semantic_scholar", error_code="rate_limited", signature="paper:rate")
        target_id = None
        for ch in ("a", "b", "c"):
            target_id = store.upsert(repeated, run_id=ch * 32, artifact_name="papers.json",
                                     artifact_sha256=ch * 64, outcome_status="failed")[0]
        store.upsert(Observation("failure", "unknown", "retrieval", "One-off unknown", "observed",
                                 error_code="unknown", signature="unknown"), run_id="d" * 32,
                     artifact_name="result.json", artifact_sha256="d" * 64, outcome_status="failed")

        plan = plan_improvement(sandbox)
        results.append({"case_id": "priority_selection", "passed": plan["target"]["memory_id"] == target_id})

        hypothesis = research_memory(sandbox, target_id)
        status = MemoryStore(sandbox).show(target_id)["status"]
        results.append({"case_id": "research_transition", "passed": status == "researched"})

        same = research_memory(sandbox, target_id)
        results.append({"case_id": "hypothesis_dedup", "passed": same["hypothesis_id"] == hypothesis["hypothesis_id"]
                        and same["reused"] is True})

        experiment = run_experiment(sandbox, hypothesis["hypothesis_id"], runner=_PassRunner())
        results.append({"case_id": "isolated_validation", "passed": experiment["outcome"] == "validated_existing_mitigation"
                        and MemoryStore(sandbox).show(target_id)["status"] == "validated"
                        and experiment["promotion_performed"] is False})

    passed_count = sum(bool(case["passed"]) for case in results)
    report = {
        "schema_version": 1,
        "suite_id": dataset["suite_id"],
        "kind": "synthetic_improvement_loop_regression_not_live_self_modification",
        "dataset_sha256": hashlib.sha256(dataset_raw).hexdigest(),
        "code_sha256": code_digest(),
        "passed": passed_count,
        "failed": len(results) - passed_count,
        "api_requests": 0,
        "cases": results,
        "limitations": [
            "This benchmark validates orchestration, isolation, and memory transitions, not autonomous code quality.",
            "No model, network provider, source edit, or promotion is performed.",
        ],
    }
    run = RunStore(root)
    path = run.path / "improvement-benchmark.json"
    write_json(path, report)
    run.event("benchmark_finished", suite=dataset["suite_id"], passed=report["passed"], failed=report["failed"])
    return path, report
