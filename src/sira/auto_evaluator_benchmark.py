"""Offline benchmark for Auto Benchmark / Evaluator v1.5A."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from .auto_evaluator import (
    benchmark_history_summary,
    compare_health,
    project_code_digest,
    select_benchmark_suite,
)
from .storage import RunStore, write_json


def _health(*, tests: int, cases: int, failed: int = 0, overall: bool = True):
    return {
        "overall_passed": overall,
        "tests": {"passed": overall, "test_count": tests},
        "benchmark": {
            "passed": overall and failed == 0,
            "passed_cases": cases,
            "failed_cases": failed,
        },
    }


def auto_evaluator_benchmark(root: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(root).resolve()
    results: list[dict[str, object]] = []

    explicit = select_benchmark_suite({"benchmark_suite": "memory"})
    results.append({"case_id": "explicit_suite", "passed": explicit["suite"] == "memory"})

    path_pick = select_benchmark_suite({"path": "src/sira/synthesis.py"})
    results.append({"case_id": "path_suite", "passed": path_pick["suite"] == "synthesis"})

    cap_pick = select_benchmark_suite({"capability": "paper_reading"})
    results.append({"case_id": "capability_suite", "passed": cap_pick["suite"] == "paper-reading"})

    regression = compare_health(_health(tests=10, cases=4), _health(tests=10, cases=0, failed=1, overall=False))
    results.append({"case_id": "candidate_regression", "passed": regression["classification"] == "candidate_regression"})

    shrink = compare_health(_health(tests=10, cases=4), _health(tests=9, cases=4))
    results.append({"case_id": "verification_shrinkage", "passed": shrink["classification"] == "verification_shrinkage"})

    expanded = compare_health(_health(tests=10, cases=4), _health(tests=11, cases=5))
    results.append({"case_id": "verification_expanded", "passed": expanded["classification"] == "verification_expanded"})

    equivalent = compare_health(_health(tests=10, cases=4), _health(tests=10, cases=4))
    results.append({"case_id": "verification_equivalent", "passed": equivalent["classification"] == "verification_equivalent"})

    with tempfile.TemporaryDirectory() as tmp:
        hist_root = Path(tmp)
        current = project_code_digest(hist_root)
        for index in range(2):
            path = hist_root / "runs" / f"{index:032x}" / "memory-benchmark.json"
            write_json(path, {
                "suite_id": "sira-memory-quality-v1",
                "kind": "memory_benchmark",
                "code_sha256": current,
                "passed": 23,
                "failed": 1,
            })
        history = benchmark_history_summary(hist_root, "memory")
        results.append({
            "case_id": "recurring_current_weakness",
            "passed": history["recurring_weakness"] is True
            and history["current_code_failed_reports"] == 2,
        })

    passed = sum(bool(row["passed"]) for row in results)
    report = {
        "schema_version": 1,
        "kind": "auto_evaluator_benchmark",
        "suite_id": "sira-auto-evaluator-v1.5a",
        "passed": passed,
        "failed": len(results) - passed,
        "api_requests": 0,
        "cases": results,
        "limitations": [
            "This benchmark validates deterministic selection and measured verification comparison only.",
            "It does not authorize promotion and does not modify protected Evaluator 1.",
        ],
    }
    store = RunStore(root)
    path = store.path / "auto-evaluator-benchmark.json"
    write_json(path, report)
    return path, report
