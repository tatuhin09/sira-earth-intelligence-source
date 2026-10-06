"""Offline benchmark for Evolution v1.4 measured outcome assessment."""
from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

from .evolution_assessment import EvolutionGapStore, assess_evolution_attempt
from .storage import RunStore, write_json


def evolution_assessment_benchmark(root: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(root).resolve()
    passed = 0

    promoted = assess_evolution_attempt({
        "status": "promoted", "outcome": "promotion_committed",
        "promotion_performed": True,
    })
    passed += int(promoted["classification"] == "promotion_gain" and promoted["strategy_consumed"])

    writer_error = assess_evolution_attempt({
        "status": "writer_error", "outcome": "http_503",
        "promotion_performed": False,
    })
    passed += int(writer_error["classification"] == "infrastructure_neutral" and not writer_error["strategy_consumed"])

    no_gain = assess_evolution_attempt({
        "status": "rejected_structural_goal", "outcome": "structural_goal_not_met",
        "promotion_performed": False,
        "structural_check": {
            "metric": "branch_points", "baseline": 3, "candidate": 3,
            "decision": "fail", "decision_code": "structural_goal_not_met",
        },
    })
    passed += int(no_gain["classification"] == "measured_no_gain" and no_gain["strategy_consumed"])

    rejected = assess_evolution_attempt({
        "status": "rejected", "outcome": "candidate_regression",
        "promotion_performed": False,
        "baseline": {"overall_passed": True, "tests": {"passed": True, "test_count": 10}},
        "candidate": {"overall_passed": False, "tests": {"passed": False, "test_count": 10}},
    })
    passed += int(rejected["classification"] == "candidate_rejected" and rejected["strategy_consumed"])

    rendered = repr(rejected).casefold()
    passed += int("output_tail" not in rendered and "traceback" not in rendered and "secret" not in rendered)

    with tempfile.TemporaryDirectory() as tmp:
        gap_root = Path(tmp)
        opportunity = {
            "opportunity_id": "op_" + "1" * 32,
            "fingerprint": "2" * 64,
            "type": "complex_function",
            "path": "src/sira/example.py",
            "symbol": "example",
            "source_sha256": "3" * 64,
        }
        gap = EvolutionGapStore(gap_root).record_strategy_exhaustion(
            opportunity,
            {
                "status": "strategy_exhausted",
                "attempted_strategy_count": 3,
                "candidate_strategy_count": 3,
            },
        )
        passed += int(
            gap["gap_kind"] == "strategy_exhausted"
            and gap["authority_granted"] is False
            and gap["external_access_requested"] is False
            and gap["promotion_authorized"] is False
        )

    report = {
        "schema_version": 1,
        "kind": "evolution_assessment_benchmark",
        "passed": passed,
        "failed": 6 - passed,
        "api_requests": 0,
    }
    run = RunStore(root)
    path = run.path / "evolution-assessment-benchmark.json"
    write_json(path, report)
    return path, report
