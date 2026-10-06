"""Offline benchmark for the v1.5C runtime evaluator feedback loop."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from .runtime_evaluator_feedback import (
    RuntimeEvaluatorFeedbackStore,
    runtime_evaluator_feedback_summary,
    stable_runtime_target_key,
)
from .storage import RunStore, write_json


def _opportunity(fingerprint: str) -> dict[str, object]:
    return {
        "target_kind": "opportunity",
        "opportunity_id": "op_" + fingerprint[:32],
        "fingerprint": fingerprint,
        "type": "complex_function",
        "path": "src/sira/retrieval.py",
        "symbol": "load_evidence_run",
        "capability": "retrieval",
        "benchmark_evidence": {
            "suite": "retrieval",
            "classification": "recurring_current_regression",
            "priority_boost": 120,
            "history": {
                "current_code_reports": 2,
                "current_code_failed_reports": 2,
                "current_code_passed_reports": 0,
                "stale_or_unknown_reports": 1,
                "recurring_weakness": True,
            },
        },
    }


def runtime_evaluator_feedback_benchmark(root: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(root).resolve()
    results: list[dict[str, object]] = []

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        first = _opportunity("1" * 64)
        changed = _opportunity("2" * 64)
        results.append({
            "case_id": "stable_opportunity_key",
            "passed": stable_runtime_target_key(first) == stable_runtime_target_key(changed),
        })

        memory_a = {"target_kind": "memory", "memory_id": "m_" + "1" * 32}
        memory_b = {"target_kind": "memory", "memory_id": "m_" + "2" * 32}
        results.append({
            "case_id": "memory_identity_is_distinct",
            "passed": stable_runtime_target_key(memory_a) != stable_runtime_target_key(memory_b),
        })

        store = RuntimeEvaluatorFeedbackStore(sandbox)
        promoted = store.record(
            {
                "cycle_id": "sc_" + "3" * 32,
                "status": "completed",
                "outcome": "promotion_committed",
                "promotion_performed": True,
                "main_tree_modified": True,
                "error": {"message": "secret raw traceback"},
            },
            first,
        )
        results.append({
            "case_id": "promotion_feedback_recorded",
            "passed": promoted["classification"] == "promotion_committed",
        })

        rendered = json.dumps(promoted, sort_keys=True).casefold()
        results.append({
            "case_id": "feedback_is_aggregate_only",
            "passed": (
                "secret raw traceback" not in rendered
                and "output_tail" not in rendered
                and promoted["authority_granted"] is False
            ),
        })

        store.record(
            {
                "cycle_id": "sc_" + "4" * 32,
                "status": "completed",
                "outcome": "structural_goal_not_met",
                "promotion_performed": False,
                "main_tree_modified": False,
            },
            changed,
        )
        summary = store.summary(first)
        results.append({
            "case_id": "stable_lineage_history_aggregates",
            "passed": summary["cycle_count"] == 2 and summary["promotion_count"] == 1,
        })

        results.append({
            "case_id": "summary_does_not_rank",
            "passed": summary["used_for_ranking"] is False,
        })

        results.append({
            "case_id": "summary_has_no_authority",
            "passed": (
                summary["authority_granted"] is False
                and summary["promotion_authorized"] is False
                and summary["external_access_requested"] is False
            ),
        })

        fresh = runtime_evaluator_feedback_summary(
            sandbox,
            {"target_kind": "memory", "memory_id": "m_" + "9" * 32},
        )
        results.append({
            "case_id": "no_history_is_safe_empty_context",
            "passed": fresh["cycle_count"] == 0 and fresh["history_applied"] is False,
        })

    passed = sum(bool(row["passed"]) for row in results)
    report = {
        "schema_version": 1,
        "kind": "runtime_evaluator_feedback_benchmark",
        "suite_id": "sira-runtime-evaluator-feedback-v1.5c",
        "passed": passed,
        "failed": len(results) - passed,
        "api_requests": 0,
        "cases": results,
        "limitations": [
            "Runtime evaluator feedback is audit/context only and never changes ranking.",
            "Protected evaluator and promotion authority remain external to this feedback layer.",
        ],
    }
    store = RunStore(root)
    path = store.path / "runtime-evaluator-feedback-benchmark.json"
    write_json(path, report)
    return path, report
