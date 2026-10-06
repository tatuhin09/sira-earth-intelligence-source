"""Protected bounded real-runtime soak controller for SIRA v1.8K.

The soak controller uses the production autonomous cycle and production
run_autonomous_loop, but runs synchronously in the foreground with strict cycle
and wall-clock bounds. It creates no new promotion authority and never bypasses
access, metered-budget, worker, evaluator, authorization, or rollback gates.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from .engineering_authorization import _surface_snapshot
from .models import utc_now
from .runtime import (
    RuntimeStateStore,
    run_autonomous_loop,
    run_unified_improvement_cycle,
)
from .self_modification import PROTECTED_PATHS
from .storage import write_json

RUNTIME_SOAK_POLICY_VERSION = 1
MIN_SOAK_CYCLES = 1
MAX_SOAK_CYCLES = 3
MIN_SOAK_SECONDS = 30
MAX_SOAK_SECONDS = 1800
DEFAULT_SOAK_CYCLES = 2
DEFAULT_SOAK_SECONDS = 900

CycleRunner = Callable[..., dict[str, Any]]
SleepFn = Callable[[float], None]
ClockFn = Callable[[], float]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _protected_snapshot(root: Path) -> dict[str, object]:
    root = Path(root).resolve()
    present: dict[str, str] = {}
    missing: list[str] = []
    unsafe: list[str] = []
    for relative in sorted(PROTECTED_PATHS):
        path = root / relative
        if not path.exists() and not path.is_symlink():
            missing.append(relative)
            continue
        if path.is_symlink() or not path.is_file():
            unsafe.append(relative)
            continue
        present[relative] = _sha256_file(path)
    canonical = "\n".join(
        f"{key}:{present[key]}"
        for key in sorted(present)
    ).encode("utf-8")
    return {
        "digest": hashlib.sha256(canonical).hexdigest(),
        "present": present,
        "reserved_missing": missing,
        "unsafe": unsafe,
    }


def _safe_resource_usage(cycle: Mapping[str, object]) -> dict[str, int]:
    raw = cycle.get("resource_usage")
    raw = raw if isinstance(raw, Mapping) else {}
    result: dict[str, int] = {}
    for key in (
        "api_requests",
        "metered_model_requests",
        "paid_requests",
    ):
        value = raw.get(key, 0)
        if isinstance(value, bool):
            value = 0
        try:
            number = max(0, int(value or 0))
        except (TypeError, ValueError):
            number = 0
        result[key] = number
    return result


def _cycle_summary(cycle: Mapping[str, object], index: int) -> dict[str, object]:
    summary: dict[str, object] = {
        "cycle_index": index,
        "status": cycle.get("status"),
        "outcome": cycle.get("outcome"),
        "target_kind": cycle.get("target_kind"),
        "promotion_performed": bool(
            cycle.get("promotion_performed", False)
        ),
        "main_tree_modified": bool(
            cycle.get("main_tree_modified", False)
        ),
        "resource_usage": _safe_resource_usage(cycle),
    }
    for key in (
        "cycle_id",
        "target_selection_id",
        "memory_id",
        "learning_goal_id",
        "opportunity_id",
        "evidence_id",
        "research_id",
        "handoff_id",
        "hypothesis_id",
        "experiment_id",
        "promotion_id",
    ):
        value = cycle.get(key)
        if isinstance(value, str) and len(value) <= 160:
            summary[key] = value
        else:
            summary[key] = None
    return summary


def _validate_limits(max_cycles: int, max_seconds: int) -> tuple[int, int]:
    if (
        isinstance(max_cycles, bool)
        or not isinstance(max_cycles, int)
        or not MIN_SOAK_CYCLES <= max_cycles <= MAX_SOAK_CYCLES
    ):
        raise ValueError(
            f"max_cycles must be an integer from "
            f"{MIN_SOAK_CYCLES} to {MAX_SOAK_CYCLES}"
        )
    if (
        isinstance(max_seconds, bool)
        or not isinstance(max_seconds, int)
        or not MIN_SOAK_SECONDS <= max_seconds <= MAX_SOAK_SECONDS
    ):
        raise ValueError(
            f"max_seconds must be an integer from "
            f"{MIN_SOAK_SECONDS} to {MAX_SOAK_SECONDS}"
        )
    return max_cycles, max_seconds


def run_bounded_real_soak(
    root: Path,
    *,
    max_cycles: int = DEFAULT_SOAK_CYCLES,
    max_seconds: int = DEFAULT_SOAK_SECONDS,
    cycle_runner: CycleRunner = run_unified_improvement_cycle,
    sleep_fn: SleepFn = time.sleep,
    clock: ClockFn = time.monotonic,
) -> dict[str, object]:
    """Run a bounded foreground soak using SIRA's real production cycle."""
    max_cycles, max_seconds = _validate_limits(
        max_cycles,
        max_seconds,
    )
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("SIRA root must be an existing directory")

    store = RuntimeStateStore(root)
    before_status = store.status()
    if not (
        before_status.get("desired_state") == "off"
        and before_status.get("effective_state") == "stopped"
        and before_status.get("worker_alive") is False
    ):
        return {
            "schema": "sira.runtime_soak_report.v1",
            "policy_version": RUNTIME_SOAK_POLICY_VERSION,
            "kind": "bounded_real_runtime_soak",
            "created_at": utc_now(),
            "status": "blocked",
            "decision_code": "runtime_must_be_off",
            "max_cycles": max_cycles,
            "max_seconds": max_seconds,
            "cycles_completed": 0,
            "runtime_before": before_status,
            "runtime_after": before_status,
            "artifact": None,
        }

    protected_before = _protected_snapshot(root)
    if protected_before["unsafe"]:
        return {
            "schema": "sira.runtime_soak_report.v1",
            "policy_version": RUNTIME_SOAK_POLICY_VERSION,
            "kind": "bounded_real_runtime_soak",
            "created_at": utc_now(),
            "status": "blocked",
            "decision_code": "protected_surface_unsafe",
            "max_cycles": max_cycles,
            "max_seconds": max_seconds,
            "cycles_completed": 0,
            "runtime_before": before_status,
            "runtime_after": before_status,
            "protected_before": protected_before,
            "artifact": None,
        }

    source_before, _ = _surface_snapshot(root)
    generation = int(before_status.get("generation") or 0) + 1
    started_at = utc_now()
    store.save({
        "desired_state": "on",
        "worker_state": "starting",
        "pid": None,
        "started_at": started_at,
        "heartbeat_at": started_at,
        "generation": generation,
    })

    start_clock = float(clock())
    deadline = start_clock + float(max_seconds)
    summaries: list[dict[str, object]] = []
    deadline_reached = False

    def stop_state(reason: str) -> None:
        current, health = store._load_raw()
        if (
            health == "ok"
            and current.get("generation") == generation
        ):
            store.save({
                "desired_state": "off",
                "worker_state": "stopping",
                "pid": os.getpid(),
                "started_at": current.get("started_at") or started_at,
                "heartbeat_at": utc_now(),
                "generation": generation,
            })

    def bounded_cycle(
        cycle_root: Path,
        cycle_generation: int,
        *,
        keep_runtime_on: bool = True,
    ) -> dict[str, Any]:
        nonlocal deadline_reached
        if float(clock()) >= deadline:
            deadline_reached = True
            stop_state("deadline_before_cycle")
            return {
                "status": "stopped_by_request",
                "outcome": "bounded_soak_deadline",
                "promotion_performed": False,
                "main_tree_modified": False,
                "resource_usage": {
                    "api_requests": 0,
                    "metered_model_requests": 0,
                    "paid_requests": 0,
                },
            }

        cycle = cycle_runner(
            cycle_root,
            cycle_generation,
            keep_runtime_on=keep_runtime_on,
        )
        if not isinstance(cycle, Mapping):
            raise ValueError("production cycle returned invalid data")
        cycle = dict(cycle)
        summaries.append(
            _cycle_summary(cycle, len(summaries) + 1)
        )
        if len(summaries) >= max_cycles:
            stop_state("cycle_limit_reached")
        return cycle

    def bounded_sleep(seconds: float) -> None:
        nonlocal deadline_reached
        remaining = max(0.0, deadline - float(clock()))
        if remaining <= 0:
            deadline_reached = True
            stop_state("deadline_during_wait")
            return
        delay = min(max(0.0, float(seconds)), remaining)
        sleep_fn(delay)
        if float(clock()) >= deadline:
            deadline_reached = True
            stop_state("deadline_after_wait")

    loop_error: dict[str, str] | None = None
    try:
        loop_report = run_autonomous_loop(
            root,
            generation,
            cycle_interval=1.0,
            sleep_fn=bounded_sleep,
            cycle_runner=bounded_cycle,
        )
    except Exception as exc:
        loop_error = {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        }
        loop_report = {
            "status": "worker_error",
            "generation": generation,
            "cycles_completed": len(summaries),
        }
    finally:
        current, health = store._load_raw()
        if (
            health == "ok"
            and current.get("generation") == generation
            and current.get("desired_state") != "off"
        ):
            store.save({
                "desired_state": "off",
                "worker_state": "stopped",
                "pid": None,
                "started_at": current.get("started_at") or started_at,
                "heartbeat_at": utc_now(),
                "generation": generation,
            })

    runtime_after = store.status()
    protected_after = _protected_snapshot(root)
    source_after, _ = _surface_snapshot(root)

    failed_cycle = any(
        row.get("status") in {
            "failed",
            "rollback_failed",
            "worker_error",
        }
        or row.get("outcome") in {
            "cycle_error",
            "rollback_failed",
        }
        for row in summaries
    )
    unauthorized_mutation = bool(
        source_after != source_before
        and not any(
            row.get("promotion_performed") is True
            and row.get("main_tree_modified") is True
            for row in summaries
        )
    )
    protected_unchanged = bool(
        protected_after["digest"] == protected_before["digest"]
        and protected_after["present"] == protected_before["present"]
        and protected_after["unsafe"] == []
    )
    runtime_clean_stop = bool(
        runtime_after.get("desired_state") == "off"
        and runtime_after.get("effective_state") == "stopped"
        and runtime_after.get("worker_alive") is False
    )
    loop_status_ok = loop_report.get("status") == "stopped"
    completed_any_cycle = len(summaries) >= 1

    passed = bool(
        loop_error is None
        and loop_status_ok
        and completed_any_cycle
        and not failed_cycle
        and not unauthorized_mutation
        and protected_unchanged
        and runtime_clean_stop
    )

    resource_totals = {
        "api_requests": sum(
            int(row["resource_usage"]["api_requests"])
            for row in summaries
        ),
        "metered_model_requests": sum(
            int(row["resource_usage"]["metered_model_requests"])
            for row in summaries
        ),
        "paid_requests": sum(
            int(row["resource_usage"]["paid_requests"])
            for row in summaries
        ),
    }

    report: dict[str, object] = {
        "schema": "sira.runtime_soak_report.v1",
        "policy_version": RUNTIME_SOAK_POLICY_VERSION,
        "kind": "bounded_real_runtime_soak",
        "created_at": utc_now(),
        "status": "passed" if passed else "failed",
        "decision_code": (
            "bounded_real_soak_passed"
            if passed
            else (
                "bounded_real_soak_inconclusive_timeout"
                if deadline_reached and not completed_any_cycle
                else "bounded_real_soak_failed"
            )
        ),
        "generation": generation,
        "max_cycles": max_cycles,
        "max_seconds": max_seconds,
        "cycles_completed": len(summaries),
        "deadline_reached": deadline_reached,
        "cycles": summaries,
        "loop": {
            "status": loop_report.get("status"),
            "cycles_completed": loop_report.get("cycles_completed"),
            "recovery": loop_report.get("recovery"),
            "multi_worker_recovery": loop_report.get(
                "multi_worker_recovery"
            ),
        },
        "checks": {
            "completed_at_least_one_cycle": completed_any_cycle,
            "loop_stopped_cleanly": loop_status_ok,
            "runtime_off_after": runtime_clean_stop,
            "protected_surface_unchanged": protected_unchanged,
            "no_failed_cycle": not failed_cycle,
            "no_unauthorized_source_mutation": not unauthorized_mutation,
        },
        "coverage": {
            "promotion_observed": any(
                row.get("promotion_performed") is True
                for row in summaries
            ),
            "main_tree_change_observed": (
                source_after != source_before
            ),
            "access_or_budget_deferral_observed": any(
                row.get("status")
                in {
                    "deferred_owner_access",
                    "deferred_resource_budget",
                }
                for row in summaries
            ),
            "idle_or_nonmutating_cycle_observed": any(
                row.get("promotion_performed") is False
                for row in summaries
            ),
        },
        "resource_usage": resource_totals,
        "paid_spending_authority": False,
        "source_sha256_before": source_before,
        "source_sha256_after": source_after,
        "protected_sha256_before": protected_before["digest"],
        "protected_sha256_after": protected_after["digest"],
        "runtime_before": before_status,
        "runtime_after": runtime_after,
        "loop_error": loop_error,
        "artifact": None,
    }

    artifact = (
        root
        / "runtime"
        / "soak"
        / f"bounded_soak_{uuid4().hex}.json"
    )
    report["artifact"] = str(artifact)
    write_json(artifact, report)
    try:
        os.chmod(artifact, 0o600)
    except OSError:
        pass
    return report
