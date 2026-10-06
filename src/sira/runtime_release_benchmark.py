from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile

from .runtime import RuntimeStateStore
from .runtime_release import run_release_acceptance
from .storage import RunStore, write_json


def _soak(root: Path, *, cycles: int, generation: int):
    path = (
        root
        / "runtime"
        / "soak"
        / f"bounded_soak_{generation:032x}.json"
    )
    payload = {
        "schema": "sira.runtime_soak_report.v1",
        "status": "passed",
        "decision_code": "bounded_real_soak_passed",
        "created_at": f"2026-09-21T10:{generation:02d}:00+00:00",
        "generation": generation,
        "cycles_completed": cycles,
        "checks": {
            "completed_at_least_one_cycle": True,
            "loop_stopped_cleanly": True,
            "runtime_off_after": True,
            "protected_surface_unchanged": True,
            "no_failed_cycle": True,
            "no_unauthorized_source_mutation": True,
        },
        "coverage": {"promotion_observed": False},
    }
    write_json(path, payload)


def _memory_stub(_root: Path):
    return {
        "primary": {"healthy": True},
        "backup": {"healthy": True},
        "parity": True,
        "migration_ready": True,
    }


def _diag(_root: Path):
    return {
        "status": "passed",
        "passed": 21,
        "failed": 0,
        "api_requests": 0,
        "metered_model_requests": 0,
        "artifact": None,
    }


def _workers(_root: Path):
    return {
        "status": "passed",
        "api_requests": 0,
        "artifact": None,
    }


def runtime_release_benchmark(root: Path):
    cases = []

    def add(case_id, passed):
        cases.append({
            "case_id": case_id,
            "passed": bool(passed),
        })

    with tempfile.TemporaryDirectory(
        prefix="sira-release-benchmark-"
    ) as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        RuntimeStateStore(project).save({
            "desired_state": "off",
            "worker_state": "stopped",
            "pid": None,
            "started_at": "2026-09-21T10:00:00+00:00",
            "heartbeat_at": "2026-09-21T10:00:00+00:00",
            "generation": 2,
        })
        _soak(project, cycles=1, generation=1)
        _soak(project, cycles=2, generation=2)

        report = run_release_acceptance(
            project,
            require_git_clean=True,
            canary_runner=_diag,
            reliability_runner=_diag,
            workers_runner=_workers,
            git_probe=lambda _root: {
                "available": True,
                "clean": True,
                "revision": "fixture",
            },
            memory_inspector=_memory_stub,
        )

        add("release_passed", report["status"] == "passed")
        add("release_ready", report["release_ready"] is True)
        add(
            "two_soaks",
            report["checks"]["two_real_soaks_passed"] is True,
        )
        add(
            "multicycle_soak",
            report["checks"]["multicycle_real_soak_passed"] is True,
        )
        add(
            "runtime_off",
            report["checks"]["runtime_off_after"] is True,
        )
        add(
            "protected_unchanged",
            report["checks"]["protected_surface_unchanged"] is True,
        )
        add(
            "source_unchanged",
            report["checks"]["source_surface_unchanged"] is True,
        )
        add(
            "memory_parity",
            report["checks"]["memory_primary_backup_parity"] is True,
        )
        add(
            "canary",
            report["checks"]["engineering_canary_21_21"] is True,
        )
        add(
            "reliability",
            report["checks"]["reliability_diagnostic_passed"] is True,
        )
        add(
            "workers",
            report["checks"]["multi_worker_diagnostic_passed"] is True,
        )
        add(
            "zero_external",
            report["external_api_requests"] == 0
            and report["metered_model_requests"] == 0,
        )

    result = {
        "schema_version": 1,
        "kind": "runtime_release_benchmark",
        "suite_id": "sira-runtime-release-v1.8l",
        "passed": sum(row["passed"] for row in cases),
        "failed": sum(not row["passed"] for row in cases),
        "api_requests": 0,
        "cases": cases,
    }
    store = RunStore(Path(root).resolve())
    path = store.path / "runtime-release-benchmark.json"
    write_json(path, result)
    return path, result
