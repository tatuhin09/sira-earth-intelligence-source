from __future__ import annotations

from pathlib import Path
import tempfile

from .runtime_soak import run_bounded_real_soak
from .storage import RunStore, write_json


def _write_fixture(root: Path):
    path = root / "src/app.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("VALUE = 1\n", encoding="utf-8")


def runtime_soak_benchmark(root: Path):
    cases = []

    def add(case_id, passed):
        cases.append({
            "case_id": case_id,
            "passed": bool(passed),
        })

    with tempfile.TemporaryDirectory(
        prefix="sira-runtime-soak-benchmark-"
    ) as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        _write_fixture(project)

        calls = []

        def cycle_runner(
            cycle_root,
            generation,
            *,
            keep_runtime_on=True,
        ):
            calls.append(generation)
            return {
                "status": "completed",
                "outcome": "validated_existing_mitigation",
                "promotion_performed": False,
                "main_tree_modified": False,
                "resource_usage": {
                    "api_requests": 0,
                    "metered_model_requests": 0,
                    "paid_requests": 0,
                },
            }

        report = run_bounded_real_soak(
            project,
            max_cycles=2,
            max_seconds=60,
            cycle_runner=cycle_runner,
            sleep_fn=lambda _seconds: None,
        )

        add("status_passed", report["status"] == "passed")
        add(
            "decision_passed",
            report["decision_code"] == "bounded_real_soak_passed",
        )
        add("two_cycles", report["cycles_completed"] == 2)
        add("cycle_limit_respected", len(calls) == 2)
        add(
            "runtime_off_after",
            report["checks"]["runtime_off_after"] is True,
        )
        add(
            "protected_unchanged",
            report["checks"]["protected_surface_unchanged"] is True,
        )
        add(
            "no_failed_cycle",
            report["checks"]["no_failed_cycle"] is True,
        )
        add(
            "no_unauthorized_mutation",
            report["checks"]["no_unauthorized_source_mutation"] is True,
        )
        add(
            "zero_metered",
            report["resource_usage"]["metered_model_requests"] == 0,
        )
        add(
            "no_paid_authority",
            report["paid_spending_authority"] is False,
        )
        add(
            "artifact_present",
            Path(report["artifact"]).is_file(),
        )
        add(
            "foreground_stopped",
            report["loop"]["status"] == "stopped",
        )

    result = {
        "schema_version": 1,
        "kind": "runtime_soak_benchmark",
        "suite_id": "sira-runtime-soak-v1.8k",
        "passed": sum(row["passed"] for row in cases),
        "failed": sum(not row["passed"] for row in cases),
        "api_requests": 0,
        "cases": cases,
    }
    store = RunStore(Path(root).resolve())
    path = store.path / "runtime-soak-benchmark.json"
    write_json(path, result)
    return path, result
