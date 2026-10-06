"""Offline benchmark for benchmark-aware prioritization v1.5B."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from .auto_evaluator import (
    benchmark_evidence_for_target,
    project_code_digest,
)
from .autonomous_targeting import select_autonomous_target
from .opportunity import discover_opportunities
from .storage import RunStore, write_json


def _memory(memory_id: str) -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "kind": "failure",
        "status": "observed",
        "category": "benchmark_regression",
        "capability": "retrieval",
        "provider": "sira-retrieval-fixture-v1",
        "occurrence_count": 1,
        "priority_score": 20,
        "last_seen_at": "2030-01-01T00:00:00+00:00",
    }


def _discovery() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "opportunity_discovery",
        "opportunities": [],
        "eligible_count": 0,
        "suppressed_cooldown_count": 0,
        "api_requests": 0,
        "paid_spending": False,
    }


def _complex_source() -> str:
    body = ["def load_evidence_run(value):"]
    for idx in range(14):
        body.extend([f"    if value == {idx}:", f"        value += {idx + 1}"])
    body.extend([f"    value += {idx}" for idx in range(20, 48)])
    body.append("    return value")
    return "\n".join(body) + "\n"


def _write_report(root: Path, name: str, *, failed: int, digest: str | None = None) -> None:
    write_json(
        root / "runs" / name / "benchmark.json",
        {
            "schema_version": 1,
            "kind": "synthetic_regression_not_live_quality",
            "suite_id": "sira-retrieval-fixture-v1",
            "passed": max(0, 3 - failed),
            "failed": failed,
            "code_sha256": digest or project_code_digest(root),
            "api_requests": 0,
        },
    )


def benchmark_intelligence_benchmark(root: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(root).resolve()
    results: list[dict[str, object]] = []

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        (sandbox / "src/sira").mkdir(parents=True)
        (sandbox / "tests").mkdir()
        (sandbox / "benchmarks").mkdir()
        (sandbox / "docs").mkdir()
        (sandbox / "src/sira/retrieval.py").write_text(_complex_source(), encoding="utf-8")

        target = {"path": "src/sira/retrieval.py", "capability": "retrieval"}
        _write_report(sandbox, "one", failed=1)
        one = benchmark_evidence_for_target(sandbox, target)
        results.append({
            "case_id": "one_off_no_boost",
            "passed": one["classification"] == "one_off_current_regression"
            and one["priority_boost"] == 0,
        })

        stale_digest = "0" * 64
        _write_report(sandbox, "stale", failed=3, digest=stale_digest)
        stale_checked = benchmark_evidence_for_target(sandbox, target)
        results.append({
            "case_id": "stale_ignored",
            "passed": stale_checked["history"]["stale_or_unknown_reports"] == 1
            and stale_checked["priority_boost"] == 0,
        })

        memory = _memory("m_" + "1" * 32)
        one_target = select_autonomous_target(
            sandbox,
            now_epoch=2_000_000_000,
            memory_candidates_fn=lambda _r, _l: [memory],
            opportunity_discovery_fn=lambda _r, _l, _n: _discovery(),
        )["target"]
        results.append({
            "case_id": "one_off_memory_not_special",
            "passed": one_target["priority_class"] == 1,
        })

        first_scan = discover_opportunities(sandbox, limit=20, now_epoch=2_000_000_000)
        first_row = next(
            row for row in first_scan["opportunities"]
            if row["type"] == "complex_function"
        )
        results.append({
            "case_id": "one_off_opportunity_no_boost",
            "passed": first_row["ranking"]["benchmark_boost"] == 0,
        })

        _write_report(sandbox, "two", failed=1)
        recurring = benchmark_evidence_for_target(sandbox, target)
        results.append({
            "case_id": "recurring_detected",
            "passed": recurring["classification"] == "recurring_current_regression"
            and 0 < recurring["priority_boost"] <= 160,
        })

        recurring_target = select_autonomous_target(
            sandbox,
            now_epoch=2_000_000_001,
            memory_candidates_fn=lambda _r, _l: [memory],
            opportunity_discovery_fn=lambda _r, _l, _n: _discovery(),
        )["target"]
        results.append({
            "case_id": "recurring_memory_special_priority",
            "passed": recurring_target["priority_class"] == 0
            and recurring_target["benchmark_evidence"]["authority_granted"] is False,
        })

        second_scan = discover_opportunities(sandbox, limit=20, now_epoch=2_000_000_002)
        second_row = next(
            row for row in second_scan["opportunities"]
            if row["type"] == "complex_function"
        )
        results.append({
            "case_id": "recurring_opportunity_boost",
            "passed": second_row["ranking"]["benchmark_boost"] > 0
            and second_row["historical_context"]["benchmark"]["recurring_weakness"] is True,
        })

        rendered = json.dumps(recurring_target, sort_keys=True).casefold()
        results.append({
            "case_id": "evidence_non_authoritative",
            "passed": (
                recurring_target["benchmark_evidence"]["promotion_authorized"] is False
                and recurring_target["benchmark_evidence"]["external_access_requested"] is False
                and "output_tail" not in rendered
                and "traceback" not in rendered
            ),
        })

    passed = sum(bool(row["passed"]) for row in results)
    report = {
        "schema_version": 1,
        "kind": "benchmark_intelligence_benchmark",
        "suite_id": "sira-benchmark-intelligence-v1.5b",
        "passed": passed,
        "failed": len(results) - passed,
        "api_requests": 0,
        "cases": results,
        "limitations": [
            "Benchmark recurrence adjusts prioritization only.",
            "It cannot authorize code changes or bypass protected evaluators.",
        ],
    }
    store = RunStore(root)
    path = store.path / "benchmark-intelligence-benchmark.json"
    write_json(path, report)
    return path, report
