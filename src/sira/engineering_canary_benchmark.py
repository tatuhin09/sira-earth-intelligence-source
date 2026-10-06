from __future__ import annotations

from pathlib import Path
import tempfile

from .engineering_canary import (
    EXPECTED_CHECK_COUNT,
    run_engineering_canary_check,
)
from .storage import RunStore, write_json


def engineering_canary_benchmark(root: Path):
    cases = []

    def add(case_id, value):
        cases.append({
            "case_id": case_id,
            "passed": bool(value),
        })

    with tempfile.TemporaryDirectory(
        prefix="sira-canary-benchmark-"
    ) as tmp:
        check_root = Path(tmp) / "project"
        check_root.mkdir()
        report = run_engineering_canary_check(
            check_root
        )

        checks = report.get("checks", {})
        soak = report.get("soak_readiness", {})
        add("status_passed", report.get("status") == "passed")
        add(
            "soak_ready",
            soak.get("ready_for_bounded_soak") is True,
        )
        add(
            "check_count_exact",
            report.get("expected_checks") == EXPECTED_CHECK_COUNT
            and len(checks) == EXPECTED_CHECK_COUNT,
        )
        add(
            "all_checks_true",
            all(value is True for value in checks.values()),
        )
        add("zero_api", report.get("api_requests") == 0)
        add(
            "zero_metered",
            report.get("metered_model_requests") == 0,
        )
        add(
            "zero_paid",
            report.get("paid_spending") is False,
        )
        add(
            "runtime_off_before",
            checks.get("runtime_off_before") is True,
        )
        add(
            "runtime_off_after",
            checks.get("runtime_off_after") is True,
        )
        add(
            "source_unchanged",
            checks.get("source_digest_unchanged_after") is True,
        )
        add(
            "rollback_verified",
            checks.get("rollback_checksum_verified") is True,
        )
        add(
            "loop_stop_verified",
            checks.get("autonomous_loop_one_cycle_stopped") is True,
        )

    result = {
        "schema_version": 1,
        "kind": "engineering_canary_benchmark",
        "suite_id": "sira-engineering-canary-v1.8j",
        "passed": sum(row["passed"] for row in cases),
        "failed": sum(not row["passed"] for row in cases),
        "api_requests": 0,
        "cases": cases,
    }
    store = RunStore(Path(root).resolve())
    path = (
        store.path
        / "engineering-canary-benchmark.json"
    )
    write_json(path, result)
    return path, result
