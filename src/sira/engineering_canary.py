"""Controlled offline canary for the autonomous engineering runtime.

The canary never starts SIRA's persistent worker and never promotes into the
real project.  It exercises the protected engineering writer/evaluator/
authorization/promotion chain only inside temporary fixture projects, then
checks the real project runtime state and source checksum before declaring
bounded-soak readiness.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping
from uuid import uuid4

from .code_writer import CodeModelBatch
from .engineering_authorization import _surface_snapshot
from .engineering_promotion import promote_engineering_candidate
from .engineering_runtime import (
    engineering_runtime_eligible,
    run_engineering_runtime_handoff,
)
from .engineering_writer import EngineeringResearchBackedWriter
from .models import utc_now
from .runtime import RuntimeStateStore, run_autonomous_loop
from .runtime_reliability import RuntimeReliabilityStore, run_reliability_check
from .storage import write_json
from .worker_coordination import run_multi_worker_check

ENGINEERING_CANARY_POLICY_VERSION = 1
EXPECTED_CHECK_COUNT = 21


class _FixtureModel:
    name = "fixture_engineering_canary_model"
    model_id = "fixture-v1"

    def __init__(self, value: int):
        self.value = int(value)

    def generate_patch(self, payload, schema):
        return CodeModelBatch(
            {
                "summary": "controlled canary source change",
                "edits": [{
                    "path": "src/app.py",
                    "content": (
                        "def value():\n"
                        f"    return {self.value}\n"
                    ),
                    "reason": "offline controlled engineering canary",
                }],
                "needs_more_context": False,
                "context_requests": [],
            },
            api_requests=0,
            input_tokens=0,
            output_tokens=0,
        )


def _passing_runner(
    argv,
    *,
    cwd,
    timeout_seconds,
    max_output_bytes,
):
    return {
        "status": "passed",
        "returncode": 0,
        "timed_out": False,
        "output_limit_exceeded": False,
        "output_bytes": 0,
        "output_sha256": hashlib.sha256(b"").hexdigest(),
        "duration_ms": 1,
        "diagnostic_text": "",
    }


def _failing_runner(
    argv,
    *,
    cwd,
    timeout_seconds,
    max_output_bytes,
):
    payload = b"controlled canary verification failure"
    return {
        "status": "failed",
        "returncode": 1,
        "timed_out": False,
        "output_limit_exceeded": False,
        "output_bytes": len(payload),
        "output_sha256": hashlib.sha256(payload).hexdigest(),
        "duration_ms": 1,
        "diagnostic_text": "src/app.py:1:1: controlled canary failure",
    }


def _postverify_failing_promoter(
    main_root,
    writer_attempt,
    evaluator_report,
    authorization_report,
    transaction_root,
    *,
    command_runner=None,
    bubblewrap_path=None,
):
    """Let pre-promotion candidate verification pass, then fail post-verify."""
    return promote_engineering_candidate(
        main_root,
        writer_attempt,
        evaluator_report,
        authorization_report,
        transaction_root,
        command_runner=_failing_runner,
        bubblewrap_path=bubblewrap_path,
    )


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_fixture(base: Path, name: str) -> Path:
    root = base / name
    root.mkdir()
    _write(
        root,
        "src/app.py",
        "def value():\n"
        "    return 1\n",
    )
    _write(
        root,
        "tests/test_app.py",
        "from src.app import value\n",
    )
    return root


def _inputs():
    opportunity_id = "op_" + "c" * 32
    target = {
        "target_kind": "opportunity",
        "opportunity_id": opportunity_id,
        "path": "src/app.py",
    }
    evidence = {
        "opportunity": {
            "opportunity_id": opportunity_id,
            "fingerprint": "d" * 64,
            "type": "explicit_debt_marker",
            "path": "src/app.py",
            "symbol": "value",
            "summary": (
                "exercise the controlled autonomous engineering canary "
                "without changing the real project"
            ),
        },
        "assessment": {"decision": "research_ready"},
        "success_criteria": {
            "preserve_current_observable_behavior": True,
            "unit_tests_must_pass": True,
        },
    }
    research = {
        "research_id": "or_" + "e" * 32,
        "writer_handoff_allowed": True,
        "research_quality": {"decision": "writer_ready"},
        "paid_spending": False,
        "metered_model_requests": 0,
        "candidate_strategies": [
            "make one bounded source-only fixture change"
        ],
        "citations": [],
    }
    return target, evidence, research


def _writer_factory(value: int):
    def make(root: Path):
        return EngineeringResearchBackedWriter(
            root,
            _FixtureModel(value),
        )
    return make


def _check(
    checks: dict[str, bool],
    name: str,
    value: object,
) -> None:
    checks[name] = bool(value)


def _blocked_report(
    root: Path,
    status: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema": "sira.engineering_canary_report.v1",
        "policy_version": ENGINEERING_CANARY_POLICY_VERSION,
        "kind": "engineering_runtime_canary",
        "created_at": utc_now(),
        "status": "blocked",
        "decision_code": "runtime_must_be_off",
        "checks": {
            "runtime_off_before": False,
        },
        "passed": 0,
        "failed": 1,
        "expected_checks": EXPECTED_CHECK_COUNT,
        "soak_readiness": {
            "ready_for_bounded_soak": False,
            "persistent_runtime_started": False,
            "real_project_source_modified": False,
        },
        "runtime_before": dict(status),
        "runtime_after": dict(status),
        "api_requests": 0,
        "metered_model_requests": 0,
        "paid_spending": False,
        "artifact": None,
        "root": str(root),
    }


def run_engineering_canary_check(
    root: Path,
) -> dict[str, object]:
    """Run a one-shot offline canary; persistent autonomy remains OFF."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("SIRA root must be an existing directory")

    runtime_before = RuntimeStateStore(root).status()
    runtime_off_before = bool(
        runtime_before.get("desired_state") == "off"
        and runtime_before.get("effective_state") == "stopped"
        and runtime_before.get("worker_alive") is False
    )
    if not runtime_off_before:
        return _blocked_report(root, runtime_before)

    source_before, _ = _surface_snapshot(root)
    reliability_path = RuntimeReliabilityStore(root).path
    try:
        reliability_bytes_before = reliability_path.read_bytes()
    except FileNotFoundError:
        reliability_bytes_before = None
    checks: dict[str, bool] = {}
    _check(checks, "runtime_off_before", runtime_off_before)
    _check(
        checks,
        "source_digest_bound_before",
        isinstance(source_before, str) and len(source_before) == 64,
    )

    with tempfile.TemporaryDirectory(
        prefix="sira-engineering-canary-"
    ) as tmp:
        base = Path(tmp)
        target, evidence, research = _inputs()

        clean_root = _make_fixture(base, "clean")
        _check(
            checks,
            "engineering_route_eligible",
            engineering_runtime_eligible(
                clean_root,
                target,
                evidence,
                research,
            ),
        )
        clean = run_engineering_runtime_handoff(
            clean_root,
            target,
            evidence,
            research,
            worker_task_id="mw_canary_clean",
            runtime_guard=lambda: True,
            writer_factory=_writer_factory(2),
            command_runner=_passing_runner,
            state_root=base / "clean-state",
        )
        clean_promotion = (
            clean.get("promotion")
            if isinstance(clean.get("promotion"), Mapping)
            else {}
        )
        _check(
            checks,
            "clean_promotion_committed",
            clean.get("status") == "promoted"
            and clean.get("outcome") == "promotion_committed"
            and clean.get("promotion_performed") is True,
        )
        _check(
            checks,
            "clean_source_promoted",
            "return 2"
            in (clean_root / "src/app.py").read_text(
                encoding="utf-8"
            ),
        )
        audit_path = clean_promotion.get("audit_path")
        _check(
            checks,
            "promotion_audit_present",
            isinstance(audit_path, str)
            and Path(audit_path).is_file(),
        )
        _check(
            checks,
            "main_tree_command_execution_disabled",
            clean_promotion.get("main_tree_command_execution") is False
            and clean.get("main_tree_command_execution") is False,
        )
        _check(
            checks,
            "package_installation_disabled",
            clean_promotion.get("package_installation_performed") is False
            and clean.get("package_installation_performed") is False,
        )

        rollback_root = _make_fixture(base, "rollback")
        rollback = run_engineering_runtime_handoff(
            rollback_root,
            target,
            evidence,
            research,
            worker_task_id="mw_canary_rollback",
            runtime_guard=lambda: True,
            writer_factory=_writer_factory(2),
            promoter=_postverify_failing_promoter,
            command_runner=_passing_runner,
            state_root=base / "rollback-state",
        )
        rollback_promotion = (
            rollback.get("promotion")
            if isinstance(rollback.get("promotion"), Mapping)
            else {}
        )
        _check(
            checks,
            "rollback_triggered",
            rollback.get("status") == "rolled_back"
            and rollback_promotion.get("rollback_performed") is True,
        )
        _check(
            checks,
            "rollback_checksum_verified",
            rollback_promotion.get("rollback_verified") is True
            and rollback_promotion.get("requires_manual_recovery") is False,
        )
        _check(
            checks,
            "rollback_restored_original",
            "return 1"
            in (rollback_root / "src/app.py").read_text(
                encoding="utf-8"
            ),
        )

        stop_root = _make_fixture(base, "stop")
        stopped = run_engineering_runtime_handoff(
            stop_root,
            target,
            evidence,
            research,
            worker_task_id="mw_canary_stop",
            runtime_guard=lambda: False,
            writer_factory=_writer_factory(2),
            command_runner=_passing_runner,
            state_root=base / "stop-state",
        )
        _check(
            checks,
            "stop_guard_blocks_promotion",
            stopped.get("status") == "stopped_by_request"
            and stopped.get("outcome") == "stop_requested"
            and stopped.get("promotion_performed") is False,
        )
        _check(
            checks,
            "stop_guard_preserves_source",
            "return 1"
            in (stop_root / "src/app.py").read_text(
                encoding="utf-8"
            ),
        )

        loop_root = _make_fixture(base, "loop")
        generation = 7
        loop_store = RuntimeStateStore(loop_root)
        loop_store.save({
            "desired_state": "on",
            "worker_state": "starting",
            "pid": None,
            "started_at": utc_now(),
            "heartbeat_at": utc_now(),
            "generation": generation,
        })

        def one_cycle(
            cycle_root,
            cycle_generation,
            *,
            keep_runtime_on=True,
        ):
            RuntimeStateStore(cycle_root).save({
                "desired_state": "off",
                "worker_state": "stopping",
                "pid": os.getpid(),
                "started_at": utc_now(),
                "heartbeat_at": utc_now(),
                "generation": cycle_generation,
            })
            return {
                "schema_version": 1,
                "kind": "self_improvement_cycle",
                "status": "completed",
                "outcome": "canary_cycle_complete",
                "promotion_performed": False,
                "main_tree_modified": False,
                "resource_usage": {
                    "metered_model_requests": 0,
                },
            }

        loop_report = run_autonomous_loop(
            loop_root,
            generation,
            cycle_interval=0,
            sleep_fn=lambda _seconds: None,
            cycle_runner=one_cycle,
        )
        loop_status = RuntimeStateStore(loop_root).status()
        _check(
            checks,
            "autonomous_loop_one_cycle_stopped",
            loop_report.get("status") == "stopped"
            and loop_report.get("cycles_completed") == 1,
        )
        _check(
            checks,
            "autonomous_loop_state_off_after",
            loop_status.get("desired_state") == "off"
            and loop_status.get("effective_state") == "stopped",
        )

    reliability = run_reliability_check(root)
    _check(
        checks,
        "reliability_check_passed",
        reliability.get("status") == "passed"
        and reliability.get("api_requests") == 0
        and reliability.get("metered_model_requests") == 0,
    )

    workers = run_multi_worker_check(root)
    _check(
        checks,
        "workers_check_passed",
        workers.get("status") == "passed"
        and workers.get("api_requests") == 0,
    )

    source_after, _ = _surface_snapshot(root)
    _check(
        checks,
        "source_digest_unchanged_after",
        source_after == source_before,
    )

    runtime_after = RuntimeStateStore(root).status()
    _check(
        checks,
        "runtime_off_after",
        runtime_after.get("desired_state") == "off"
        and runtime_after.get("effective_state") == "stopped"
        and runtime_after.get("worker_alive") is False,
    )
    _check(
        checks,
        "zero_external_api_requests",
        clean.get("writer_report", {}).get("api_requests", 0) == 0
        and rollback.get("writer_report", {}).get("api_requests", 0) == 0
        and stopped.get("writer_report", {}).get("api_requests", 0) == 0,
    )
    try:
        reliability_bytes_after = reliability_path.read_bytes()
    except FileNotFoundError:
        reliability_bytes_after = None
    _check(
        checks,
        "no_metered_attempt_recorded_by_canary",
        reliability_bytes_after == reliability_bytes_before,
    )

    if len(checks) != EXPECTED_CHECK_COUNT:
        raise RuntimeError(
            "engineering canary check-count contract drifted"
        )

    passed = sum(checks.values())
    failed = EXPECTED_CHECK_COUNT - passed
    ready = failed == 0
    report: dict[str, object] = {
        "schema": "sira.engineering_canary_report.v1",
        "policy_version": ENGINEERING_CANARY_POLICY_VERSION,
        "kind": "engineering_runtime_canary",
        "created_at": utc_now(),
        "status": "passed" if ready else "failed",
        "decision_code": (
            "bounded_soak_ready"
            if ready
            else "canary_checks_failed"
        ),
        "checks": checks,
        "passed": passed,
        "failed": failed,
        "expected_checks": EXPECTED_CHECK_COUNT,
        "source_sha256_before": source_before,
        "source_sha256_after": source_after,
        "runtime_before": runtime_before,
        "runtime_after": runtime_after,
        "reliability": {
            "status": reliability.get("status"),
            "artifact": reliability.get("artifact"),
        },
        "workers": {
            "status": workers.get("status"),
            "artifact": workers.get("artifact"),
        },
        "soak_readiness": {
            "ready_for_bounded_soak": ready,
            "persistent_runtime_started": False,
            "real_project_source_modified": source_after != source_before,
            "main_tree_command_execution": False,
            "package_installation": False,
            "network_required": False,
            "model_required": False,
            "manual_self_on_required_for_future_soak": True,
        },
        "api_requests": 0,
        "metered_model_requests": 0,
        "paid_spending": False,
        "artifact": None,
        "root": str(root),
    }

    artifact = (
        root
        / "runtime"
        / "checks"
        / f"engineering_canary_{uuid4().hex}.json"
    )
    report["artifact"] = str(artifact)
    write_json(artifact, report)
    try:
        os.chmod(artifact, 0o600)
    except OSError:
        pass
    return report
