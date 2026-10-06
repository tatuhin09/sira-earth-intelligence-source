"""Protected final operations and release-acceptance gate for SIRA v1.8L."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping
from uuid import uuid4

from .engineering_authorization import _surface_snapshot
from .engineering_canary import run_engineering_canary_check
from .memory_integrity import build_integrity_report
from .models import utc_now
from .owner_notifications import OwnerNotificationStore
from .runtime import RuntimeStateStore
from .runtime_reliability import run_reliability_check
from .runtime_soak import _protected_snapshot
from .storage import write_json
from .worker_coordination import run_multi_worker_check

RELEASE_POLICY_VERSION = 1
MIN_PASSED_REAL_SOAKS = 2
MAX_SOAK_ARTIFACTS = 64
MAX_SOAK_ARTIFACT_BYTES = 2 * 1024 * 1024

DiagnosticRunner = Callable[[Path], Mapping[str, object]]
GitProbe = Callable[[Path], Mapping[str, object]]
MemoryInspector = Callable[[Path], Mapping[str, object]]


def _git_probe(root: Path) -> dict[str, object]:
    root = Path(root).resolve()
    revision = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if revision.returncode != 0 or status.returncode != 0:
        return {
            "available": False,
            "clean": False,
            "revision": None,
        }
    return {
        "available": True,
        "clean": not bool(status.stdout.strip()),
        "revision": revision.stdout.strip()[:80],
    }


def _memory_inspector(root: Path) -> dict[str, object]:
    root = Path(root).resolve()
    primary = root / "memory" / "sira_memory.sqlite3"
    backup = (
        root
        / "memory"
        / "backups"
        / "sira_memory.latest.sqlite3"
    )
    report = build_integrity_report(primary, backup)
    rendered = report.to_dict()
    if not isinstance(rendered, dict):
        raise ValueError("memory integrity report is invalid")
    return rendered


def _load_json_file(path: Path) -> dict[str, object] | None:
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > MAX_SOAK_ARTIFACT_BYTES
        ):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _soak_history(root: Path) -> dict[str, object]:
    soak_dir = Path(root).resolve() / "runtime" / "soak"
    if not soak_dir.is_dir() or soak_dir.is_symlink():
        return {
            "artifact_count": 0,
            "passed_count": 0,
            "multicycle_passed_count": 0,
            "latest": None,
            "artifacts": [],
        }

    candidates = [
        path
        for path in soak_dir.glob("bounded_soak_*.json")
        if path.is_file() and not path.is_symlink()
    ]
    candidates.sort(
        key=lambda p: (p.stat().st_mtime_ns, p.name),
        reverse=True,
    )
    rows: list[dict[str, object]] = []
    for path in candidates[:MAX_SOAK_ARTIFACTS]:
        value = _load_json_file(path)
        if (
            value is None
            or value.get("schema") != "sira.runtime_soak_report.v1"
        ):
            continue
        checks = value.get("checks")
        checks = checks if isinstance(checks, Mapping) else {}
        clean = bool(
            value.get("status") == "passed"
            and value.get("decision_code")
            == "bounded_real_soak_passed"
            and checks.get("completed_at_least_one_cycle") is True
            and checks.get("loop_stopped_cleanly") is True
            and checks.get("runtime_off_after") is True
            and checks.get("protected_surface_unchanged") is True
            and checks.get("no_failed_cycle") is True
            and checks.get("no_unauthorized_source_mutation") is True
        )
        try:
            cycles = int(value.get("cycles_completed") or 0)
        except (TypeError, ValueError):
            cycles = 0
        rows.append({
            "artifact": str(path),
            "created_at": value.get("created_at"),
            "generation": value.get("generation"),
            "cycles_completed": max(0, cycles),
            "passed": clean,
            "promotion_observed": bool(
                (
                    value.get("coverage")
                    if isinstance(value.get("coverage"), Mapping)
                    else {}
                ).get("promotion_observed", False)
            ),
        })

    passed = [row for row in rows if row["passed"] is True]
    return {
        "artifact_count": len(rows),
        "passed_count": len(passed),
        "multicycle_passed_count": sum(
            int(row["cycles_completed"]) >= 2
            for row in passed
        ),
        "latest": rows[0] if rows else None,
        "artifacts": rows[:10],
    }


def _notification_summary(root: Path) -> dict[str, object]:
    try:
        rows = OwnerNotificationStore(root).iter_events()
    except (OSError, ValueError):
        return {
            "readable": False,
            "total": 0,
            "by_status": {},
        }

    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")[:80]
        counts[status] = counts.get(status, 0) + 1
    return {
        "readable": True,
        "total": len(rows),
        "by_status": dict(sorted(counts.items())),
    }


def _last_cycle_safe(runtime_status: Mapping[str, object]) -> bool:
    last = runtime_status.get("last_cycle")
    if last is None:
        return True
    if not isinstance(last, Mapping):
        return False
    status = str(last.get("status") or "")
    outcome = str(last.get("outcome") or "")
    return (
        status not in {"failed", "rollback_failed", "worker_error"}
        and outcome not in {"cycle_error", "rollback_failed"}
    )


def run_release_acceptance(
    root: Path,
    *,
    require_git_clean: bool = True,
    canary_runner: DiagnosticRunner = run_engineering_canary_check,
    reliability_runner: DiagnosticRunner = run_reliability_check,
    workers_runner: DiagnosticRunner = run_multi_worker_check,
    git_probe: GitProbe = _git_probe,
    memory_inspector: MemoryInspector = _memory_inspector,
) -> dict[str, object]:
    """Aggregate existing protected diagnostics into a final release verdict."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("SIRA root must be an existing directory")

    runtime_before = RuntimeStateStore(root).status()
    if not (
        runtime_before.get("desired_state") == "off"
        and runtime_before.get("effective_state") == "stopped"
        and runtime_before.get("worker_alive") is False
    ):
        return {
            "schema": "sira.release_acceptance.v1",
            "policy_version": RELEASE_POLICY_VERSION,
            "kind": "final_release_acceptance",
            "created_at": utc_now(),
            "status": "blocked",
            "decision_code": "runtime_must_be_off",
            "release_ready": False,
            "runtime_before": runtime_before,
            "artifact": None,
        }

    protected_before = _protected_snapshot(root)
    source_before, _ = _surface_snapshot(root)
    git = dict(git_probe(root))
    soak = _soak_history(root)
    notifications = _notification_summary(root)

    try:
        memory = dict(memory_inspector(root))
    except Exception as exc:
        memory = {
            "error": type(exc).__name__,
            "primary": {"healthy": False},
            "backup": {"healthy": False},
            "parity": False,
        }

    canary = dict(canary_runner(root))
    reliability = dict(reliability_runner(root))
    workers = dict(workers_runner(root))

    runtime_after = RuntimeStateStore(root).status()
    protected_after = _protected_snapshot(root)
    source_after, _ = _surface_snapshot(root)

    primary = (
        memory.get("primary")
        if isinstance(memory.get("primary"), Mapping)
        else {}
    )
    backup = (
        memory.get("backup")
        if isinstance(memory.get("backup"), Mapping)
        else {}
    )
    latest_soak = (
        soak.get("latest")
        if isinstance(soak.get("latest"), Mapping)
        else {}
    )

    checks = {
        "runtime_off_before": bool(
            runtime_before.get("desired_state") == "off"
            and runtime_before.get("effective_state") == "stopped"
            and runtime_before.get("worker_alive") is False
        ),
        "runtime_state_healthy": runtime_before.get("state_health") == "ok",
        "git_repository_available": git.get("available") is True,
        "git_working_tree_clean": git.get("clean") is True,
        "protected_surface_safe_before": not bool(
            protected_before.get("unsafe")
        ),
        "engineering_canary_21_21": bool(
            canary.get("status") == "passed"
            and canary.get("passed") == 21
            and canary.get("failed") == 0
        ),
        "reliability_diagnostic_passed": bool(
            reliability.get("status") == "passed"
            and reliability.get("api_requests") == 0
            and reliability.get("metered_model_requests") == 0
        ),
        "multi_worker_diagnostic_passed": bool(
            workers.get("status") == "passed"
            and workers.get("api_requests") == 0
        ),
        "memory_primary_healthy": primary.get("healthy") is True,
        "memory_backup_healthy": backup.get("healthy") is True,
        "memory_primary_backup_parity": memory.get("parity") is True,
        "two_real_soaks_passed": int(
            soak.get("passed_count") or 0
        ) >= MIN_PASSED_REAL_SOAKS,
        "multicycle_real_soak_passed": int(
            soak.get("multicycle_passed_count") or 0
        ) >= 1,
        "latest_real_soak_passed": latest_soak.get("passed") is True,
        "last_cycle_not_failed": _last_cycle_safe(runtime_before),
        "notification_outbox_readable": notifications.get("readable") is True,
        "protected_surface_unchanged": bool(
            protected_after.get("digest")
            == protected_before.get("digest")
            and protected_after.get("present")
            == protected_before.get("present")
            and not bool(protected_after.get("unsafe"))
        ),
        "source_surface_unchanged": source_after == source_before,
        "runtime_off_after": bool(
            runtime_after.get("desired_state") == "off"
            and runtime_after.get("effective_state") == "stopped"
            and runtime_after.get("worker_alive") is False
        ),
    }

    required = dict(checks)
    if not require_git_clean:
        required.pop("git_working_tree_clean", None)

    ready = all(required.values())
    report: dict[str, object] = {
        "schema": "sira.release_acceptance.v1",
        "policy_version": RELEASE_POLICY_VERSION,
        "kind": "final_release_acceptance",
        "created_at": utc_now(),
        "status": "passed" if ready else "failed",
        "decision_code": (
            "release_ready"
            if ready and require_git_clean
            else (
                "release_preflight_passed"
                if ready
                else "release_acceptance_failed"
            )
        ),
        "release_ready": bool(ready and require_git_clean),
        "preflight_ready": ready,
        "require_git_clean": require_git_clean,
        "checks": checks,
        "required_check_count": len(required),
        "required_checks_passed": sum(
            value is True for value in required.values()
        ),
        "git": git,
        "runtime_before": runtime_before,
        "runtime_after": runtime_after,
        "memory": memory,
        "soak_history": soak,
        "notifications": notifications,
        "diagnostics": {
            "engineering_canary": {
                "status": canary.get("status"),
                "passed": canary.get("passed"),
                "failed": canary.get("failed"),
                "artifact": canary.get("artifact"),
            },
            "reliability": {
                "status": reliability.get("status"),
                "artifact": reliability.get("artifact"),
            },
            "multi_worker": {
                "status": workers.get("status"),
                "artifact": workers.get("artifact"),
            },
        },
        "integrity": {
            "source_sha256_before": source_before,
            "source_sha256_after": source_after,
            "protected_sha256_before": protected_before.get("digest"),
            "protected_sha256_after": protected_after.get("digest"),
        },
        "operations": {
            "status": "python sira.py self status",
            "start": "python sira.py self on",
            "stop": "python sira.py self off",
            "release_check": "python sira.py self release-check",
            "notifications": "python sira.py notifications list",
        },
        "authority": {
            "new_promotion_authority": False,
            "new_payment_authority": False,
            "paid_spending_authority": False,
            "package_installation_authority": False,
        },
        "external_api_requests": 0,
        "metered_model_requests": 0,
        "artifact": None,
    }

    artifact = (
        root
        / "runtime"
        / "release"
        / f"release_acceptance_{uuid4().hex}.json"
    )
    report["artifact"] = str(artifact)
    write_json(artifact, report)
    try:
        os.chmod(artifact, 0o600)
    except OSError:
        pass
    return report
