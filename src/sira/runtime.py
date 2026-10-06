"""Persistent fail-closed runtime state for SIRA's future autonomous worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from .improvement import plan_improvement, research_memory, run_experiment
from .autonomous_targeting import select_autonomous_target
from .opportunity import OpportunityStore
from .opportunity_evidence import build_opportunity_evidence
from .opportunity_research import research_opportunity_free
from .opportunity_handoff import run_opportunity_writer_handoff
from .engineering_runtime import (
    engineering_runtime_eligible,
    run_engineering_runtime_handoff,
)
from .knowledge_runtime import revalidate_knowledge_target
from .learning_goal_runtime import run_learning_goal_target
from .autonomous_promotion import run_autonomous_candidate_promotion
from .code_writer import make_default_code_writer
from .models import ProviderError, utc_now
from .storage import write_json
from .runtime_evaluator_feedback import safe_record_runtime_evaluator_feedback
from .owner_notifications import pump_owner_notifications
from .access_provisioning import reconcile_approved_access_requests
from .access_runtime import (
    access_need_from_research,
    access_need_from_broker_decision,
    blocked_target_ids,
    consume_resume_for_target,
    filter_selection_for_access,
    park_target_for_access,
)
from .runtime_reliability import (
    ActiveCycleJournal,
    RuntimeReliabilityStore,
    recover_interrupted_cycle,
)
from .runtime_activity import current_cycle_summary
from .worker_coordination import (
    MultiWorkerCoordinator,
    PromotionLease,
    recover_orphaned_worker_tasks,
    recover_stale_promotion_lease,
)

_STATE_SCHEMA_VERSION = 1
_ALLOWED_DESIRED = {"on", "off"}
_ALLOWED_WORKER = {"stopped", "starting", "running", "stopping", "error"}
_RUNTIME_STAGE = "1.2"


def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A process exists but belongs to a different principal. Treat it as
        # alive; later worker-token verification will distinguish ownership.
        return True
    except OSError:
        return False
    return True


class RuntimeStateStore:
    """Read/write runtime intent without starting or stopping a worker.

    v1.0A-1 deliberately implements status/state only. Future stages may
    mutate this state through explicit ``self on`` / ``self off`` commands.
    Missing or malformed state is always interpreted as OFF.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.runtime_dir = self.root / "runtime"
        self.path = self.runtime_dir / "self_state.json"

    @staticmethod
    def _default(health: str) -> dict[str, Any]:
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "desired_state": "off",
            "worker_state": "stopped",
            "pid": None,
            "started_at": None,
            "heartbeat_at": None,
            "generation": 0,
            "updated_at": None,
            "state_health": health,
        }

    def _load_raw(self) -> tuple[dict[str, Any], str]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._default("missing_default"), "missing_default"
        except (OSError, UnicodeError, json.JSONDecodeError):
            return self._default("corrupt_fail_closed"), "corrupt_fail_closed"

        if not isinstance(value, dict):
            return self._default("corrupt_fail_closed"), "corrupt_fail_closed"
        try:
            schema_version = value["schema_version"]
            desired_state = value["desired_state"]
            worker_state = value["worker_state"]
            pid = value.get("pid")
            generation = value["generation"]
        except KeyError:
            return self._default("corrupt_fail_closed"), "corrupt_fail_closed"

        valid_pid = pid is None or (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0)
        valid_generation = isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0
        valid_times = all(value.get(key) is None or isinstance(value.get(key), str)
                          for key in ("started_at", "heartbeat_at", "updated_at"))
        if (schema_version != _STATE_SCHEMA_VERSION or desired_state not in _ALLOWED_DESIRED
                or worker_state not in _ALLOWED_WORKER or not valid_pid
                or not valid_generation or not valid_times):
            return self._default("corrupt_fail_closed"), "corrupt_fail_closed"

        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "desired_state": desired_state,
            "worker_state": worker_state,
            "pid": pid,
            "started_at": value.get("started_at"),
            "heartbeat_at": value.get("heartbeat_at"),
            "generation": generation,
            "updated_at": value.get("updated_at"),
            "state_health": "ok",
        }, "ok"

    def save(self, state: dict[str, Any]) -> None:
        desired_state = state.get("desired_state", "off")
        worker_state = state.get("worker_state", "stopped")
        pid = state.get("pid")
        generation = state.get("generation", 0)
        if desired_state not in _ALLOWED_DESIRED:
            raise ValueError("desired_state must be 'on' or 'off'")
        if worker_state not in _ALLOWED_WORKER:
            raise ValueError("invalid worker_state")
        if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
            raise ValueError("pid must be a positive integer or null")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        for key in ("started_at", "heartbeat_at"):
            if state.get(key) is not None and not isinstance(state.get(key), str):
                raise ValueError(f"{key} must be a string or null")

        self.runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        payload = {
            "schema_version": _STATE_SCHEMA_VERSION,
            "desired_state": desired_state,
            "worker_state": worker_state,
            "pid": pid,
            "started_at": state.get("started_at"),
            "heartbeat_at": state.get("heartbeat_at"),
            "generation": generation,
            "updated_at": utc_now(),
        }
        write_json(self.path, payload)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _last_cycle_summary(self) -> dict[str, Any] | None:
        path = self.runtime_dir / "last_cycle.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("kind") != "self_improvement_cycle":
            return None
        return {
            "cycle_id": data.get("cycle_id"),
            "status": data.get("status"),
            "target_kind": data.get("target_kind"),
            "target_selection_id": data.get("target_selection_id"),
            "memory_id": data.get("memory_id"),
            "opportunity_id": data.get("opportunity_id"),
            "evidence_id": data.get("evidence_id"),
            "research_id": data.get("research_id"),
            "handoff_id": data.get("handoff_id"),
            "hypothesis_id": data.get("hypothesis_id"),
            "experiment_id": data.get("experiment_id"),
            "promotion_id": data.get("promotion_id"),
            "outcome": data.get("outcome"),
            "completed_at": data.get("completed_at"),
        }

    def status(self) -> dict[str, Any]:
        state, health = self._load_raw()
        pid = state.get("pid")
        alive = _pid_alive(pid)
        desired = state["desired_state"]
        worker_state = state["worker_state"]

        if health == "ok" and desired == "on" and worker_state in {"starting", "running", "stopping"} and not alive:
            health = "stale_worker"
        effective = "running" if health == "ok" and desired == "on" and worker_state == "running" and alive else "stopped"

        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "kind": "self_status",
            "sira_runtime_stage": _RUNTIME_STAGE,
            "desired_state": desired if health not in {"corrupt_fail_closed"} else "off",
            "effective_state": effective,
            "worker_state": worker_state if health != "corrupt_fail_closed" else "stopped",
            "worker_alive": bool(alive and health != "corrupt_fail_closed"),
            "pid": pid if health != "corrupt_fail_closed" else None,
            "started_at": state.get("started_at") if health != "corrupt_fail_closed" else None,
            "heartbeat_at": state.get("heartbeat_at") if health != "corrupt_fail_closed" else None,
            "generation": state.get("generation", 0) if health != "corrupt_fail_closed" else 0,
            "state_health": health,
            "state_file": str(self.path),
            "active_cycle": current_cycle_summary(self.root, state["generation"]) if effective == "running" else None,
            "last_cycle": self._last_cycle_summary(),
        }


class WorkerProcess(Protocol):
    pid: int


class WorkerLauncher(Protocol):
    def launch(self, root: Path, generation: int) -> WorkerProcess: ...


class SubprocessWorkerLauncher:
    """Launch one detached persistent autonomous improvement worker."""

    def launch(self, root: Path, generation: int) -> subprocess.Popen:
        root = Path(root).resolve()
        runtime_dir = root / "runtime"
        runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        log_path = runtime_dir / "worker.log"
        env = dict(os.environ)
        # The detached worker deliberately does not inherit provider credentials.
        # The code writer may load GEMINI_API_KEY from the main project .env;
        # candidate workspaces never receive that file or these environment keys.
        for name in ("TAVILY_API_KEY", "GEMINI_API_KEY", "SIRA_SEMANTIC_SCHOLAR_API_KEY"):
            env.pop(name, None)
        env["PYTHONUNBUFFERED"] = "1"
        command = [
            sys.executable,
            str(root / "sira.py"),
            "--root", str(root),
            "_self-worker",
            "--generation", str(generation),
        ]
        with log_path.open("ab", buffering=0) as log:
            return subprocess.Popen(
                command,
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )


def run_heartbeat_probe(
    root: Path,
    generation: int,
    *,
    heartbeat_count: int = 5,
    heartbeat_interval: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Claim a generation, emit bounded heartbeats, then return to OFF.

    This stage deliberately does not run research or improvement work yet.
    """
    root = Path(root).resolve()
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        raise ValueError("generation must be a positive integer")
    if not isinstance(heartbeat_count, int) or isinstance(heartbeat_count, bool) or heartbeat_count <= 0:
        raise ValueError("heartbeat_count must be a positive integer")
    if not isinstance(heartbeat_interval, (int, float)) or isinstance(heartbeat_interval, bool) or heartbeat_interval < 0:
        raise ValueError("heartbeat_interval must be non-negative")

    store = RuntimeStateStore(root)
    state = None
    health = None
    for _ in range(40):
        state, health = store._load_raw()
        if health == "ok" and state.get("generation") != generation:
            return {"status": "stale_generation", "generation": generation}
        if (health == "ok" and state.get("desired_state") == "on"
                and state.get("generation") == generation
                and state.get("pid") in (None, os.getpid())):
            break
        sleep_fn(0.05)
    else:
        return {"status": "ownership_not_acquired", "generation": generation}

    started_at = state.get("started_at") or utc_now()
    heartbeats = 0
    for index in range(heartbeat_count):
        store.save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": os.getpid(),
            "started_at": started_at,
            "heartbeat_at": utc_now(),
            "generation": generation,
        })
        heartbeats += 1
        if index + 1 < heartbeat_count:
            sleep_fn(float(heartbeat_interval))

    store.save({
        "desired_state": "off",
        "worker_state": "stopped",
        "pid": None,
        "started_at": started_at,
        "heartbeat_at": utc_now(),
        "generation": generation,
    })
    return {"status": "completed", "generation": generation, "heartbeats": heartbeats}


def run_one_improvement_cycle(
    root: Path,
    generation: int,
    *,
    planner: Callable[[Path], dict[str, Any]] = plan_improvement,
    researcher: Callable[[Path, str], dict[str, Any]] = research_memory,
    experimenter: Callable[[Path, str], dict[str, Any]] = run_experiment,
    autonomous_promoter: Callable[..., dict[str, object]] = run_autonomous_candidate_promotion,
    code_writer_factory: Callable[[Path], object] = make_default_code_writer,
    keep_runtime_on: bool = False,
) -> dict[str, Any]:
    """Run one bounded local improvement cycle and persist the result.

    ``keep_runtime_on`` is used only by the persistent worker. The cycle is
    now may invoke the configured research-backed code writer, but every
    generated edit remains inside the existing candidate -> evaluator ->
    transactional promotion/rollback chain.
    """
    root = Path(root).resolve()
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        raise ValueError("generation must be a positive integer")

    store = RuntimeStateStore(root)
    state, health = store._load_raw()
    if health != "ok" or state.get("generation") != generation:
        return {"status": "stale_generation", "generation": generation}
    if state.get("desired_state") != "on" or state.get("pid") not in (None, os.getpid()):
        return {"status": "ownership_not_acquired", "generation": generation}

    started_at = state.get("started_at") or utc_now()
    cycle_id = "sc_" + uuid4().hex
    cycle_path = store.runtime_dir / "last_cycle.json"

    def heartbeat() -> None:
        current, current_health = store._load_raw()
        if (current_health != "ok" or current.get("generation") != generation
                or current.get("desired_state") != "on"):
            if (current_health == "ok" and current.get("generation") == generation
                    and current.get("desired_state") == "off"):
                raise _StopRequested("autonomous stop requested")
            raise RuntimeError("autonomous cycle lost runtime ownership")
        store.save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": os.getpid(),
            "started_at": started_at,
            "heartbeat_at": utc_now(),
            "generation": generation,
        })

    result: dict[str, Any] = {
        "schema_version": 1,
        "kind": "self_improvement_cycle",
        "sira_runtime_stage": _RUNTIME_STAGE,
        "cycle_id": cycle_id,
        "generation": generation,
        "started_at": utc_now(),
        "completed_at": None,
        "status": "running",
        "plan_id": None,
        "memory_id": None,
        "hypothesis_id": None,
        "experiment_id": None,
        "promotion_id": None,
        "outcome": None,
        "promotion_performed": False,
        "main_tree_modified": False,
        "code_writer_attempted": False,
        "code_writer_status": None,
        "code_writer": None,
        "code_writer_error": None,
        "error": None,
    }

    try:
        heartbeat()
        try:
            plan = planner(root)
        except ValueError as error:
            if str(error).startswith("No observed or rejected failure memory"):
                result["status"] = "idle_no_candidate"
                result["outcome"] = "no_candidate"
                return result
            raise

        result["plan_id"] = plan.get("plan_id")
        target = plan.get("target") if isinstance(plan, dict) else None
        memory_id = target.get("memory_id") if isinstance(target, dict) else None
        if not isinstance(memory_id, str):
            raise ValueError("Improvement plan did not contain a memory target")
        result["memory_id"] = memory_id

        heartbeat()
        hypothesis = researcher(root, memory_id)
        hypothesis_id = hypothesis.get("hypothesis_id") if isinstance(hypothesis, dict) else None
        if not isinstance(hypothesis_id, str):
            raise ValueError("Improvement research did not produce a hypothesis")
        result["hypothesis_id"] = hypothesis_id
        if hypothesis.get("repeat_blocked"):
            result["status"] = "blocked_repeat"
            result["outcome"] = "repeat_blocked"
            return result

        heartbeat()
        structured_edits = hypothesis.get("candidate_text_edits")
        if isinstance(structured_edits, Mapping) and structured_edits:
            promotion_attempt = autonomous_promoter(root, hypothesis)
            result["outcome"] = promotion_attempt.get("outcome")
            result["promotion_performed"] = bool(promotion_attempt.get("promotion_performed", False))
            result["main_tree_modified"] = bool(promotion_attempt.get("main_tree_modified", False))
            promotion = promotion_attempt.get("promotion")
            if isinstance(promotion, Mapping):
                result["promotion_id"] = promotion.get("promotion_id")
            if promotion_attempt.get("status") == "rollback_failed":
                raise RuntimeError("autonomous promotion rollback failed")
            result["status"] = "completed"
            return result

        # Research normally produces a bounded hypothesis rather than literal code.
        # v1.0C-2 gives the persistent worker a configured code writer, but keeps
        # all generated edits inside the existing candidate/gate/promotion chain.
        result["code_writer_attempted"] = True
        try:
            writer = code_writer_factory(root)
            promotion_attempt = autonomous_promoter(root, hypothesis, writer=writer)
            writer_report = promotion_attempt.get("writer_report")
            result["code_writer"] = writer_report if isinstance(writer_report, Mapping) else None
            if promotion_attempt.get("status") != "no_edit_proposed":
                result["code_writer_status"] = "promotion_attempted"
                result["outcome"] = promotion_attempt.get("outcome")
                result["promotion_performed"] = bool(promotion_attempt.get("promotion_performed", False))
                result["main_tree_modified"] = bool(promotion_attempt.get("main_tree_modified", False))
                promotion = promotion_attempt.get("promotion")
                if isinstance(promotion, Mapping):
                    result["promotion_id"] = promotion.get("promotion_id")
                if promotion_attempt.get("status") == "rollback_failed":
                    raise RuntimeError("autonomous promotion rollback failed")
                result["status"] = "completed"
                return result
            result["code_writer_status"] = "no_edit_fallback_experiment"
        except (ProviderError, OSError, ValueError) as error:
            # A missing key, transient provider failure, invalid model payload, or
            # rejected generated edit must never modify main or kill the always-on
            # worker. Preserve the writer failure in the cycle audit and fall back
            # to the already trusted non-mutating regression experiment.
            result["code_writer_status"] = "fallback_experiment"
            result["code_writer_error"] = {
                "type": type(error).__name__,
                "message": str(error)[:500],
            }

        experiment = experimenter(root, hypothesis_id)
        result["experiment_id"] = experiment.get("experiment_id")
        result["outcome"] = experiment.get("outcome")
        result["promotion_performed"] = bool(experiment.get("promotion_performed", False))
        result["main_tree_modified"] = bool(experiment.get("main_tree_modified", False))
        if result["promotion_performed"] or result["main_tree_modified"]:
            raise RuntimeError("legacy experiment unexpectedly modified the main tree")
        result["status"] = "completed"
        return result
    except _StopRequested:
        result["status"] = "stopped_by_request"
        result["outcome"] = "stop_requested"
        return result
    except Exception as error:
        result["status"] = "failed"
        result["outcome"] = "cycle_error"
        result["error"] = {"type": type(error).__name__, "message": str(error)[:500]}
        return result
    finally:
        result["completed_at"] = utc_now()
        store.runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        write_json(cycle_path, result)
        try:
            os.chmod(cycle_path, 0o600)
        except OSError:
            pass
        current, current_health = store._load_raw()
        keep_running = (
            keep_runtime_on
            and result["status"] != "stopped_by_request"
            and current_health == "ok"
            and current.get("generation") == generation
            and current.get("desired_state") == "on"
        )
        store.save({
            "desired_state": "on" if keep_running else "off",
            "worker_state": "running" if keep_running else ("error" if result["status"] == "failed" else "stopped"),
            "pid": os.getpid() if keep_running else None,
            "started_at": started_at,
            "heartbeat_at": utc_now(),
            "generation": generation,
        })


class _StopRequested(RuntimeError):
    pass


def _metered_budget(root: Path, *, now_epoch: float | None = None) -> dict[str, Any]:
    return RuntimeReliabilityStore(root).metered_budget(now_epoch=now_epoch)


def _record_metered_attempt(root: Path, *, now_epoch: float | None = None) -> dict[str, Any]:
    return RuntimeReliabilityStore(root).record_metered_attempt(now_epoch=now_epoch)


def _code_generation_broker_preflight(root: Path) -> dict[str, Any]:
    """Preflight the default Gemini writer using credential presence only."""
    from .capability_broker import broker_decision
    from .config import load_optional_key

    configured = load_optional_key(Path(root).resolve(), "GEMINI_API_KEY") is not None
    presence_only = {"GEMINI_API_KEY": "configured"} if configured else {}
    return broker_decision(
        Path(root).resolve(),
        "code_generation",
        allow_metered=True,
        environ=presence_only,
        persist=True,
    )


def _verify_opportunity_for_worker(
    root: Path, target: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, Any]:
    """Local read-only contract verification for the parallel verification worker."""
    _ = Path(root).resolve()
    assessment = evidence.get("assessment") if isinstance(evidence.get("assessment"), Mapping) else {}
    opportunity = evidence.get("opportunity") if isinstance(evidence.get("opportunity"), Mapping) else {}
    checks = {
        "evidence_ready": assessment.get("decision") == "research_ready",
        "opportunity_id_consistent": (
            not opportunity.get("opportunity_id")
            or opportunity.get("opportunity_id") == target.get("opportunity_id")
        ),
        "target_path_consistent": (
            not opportunity.get("path") or opportunity.get("path") == target.get("path")
        ),
        "target_symbol_consistent": (
            not opportunity.get("symbol") or opportunity.get("symbol") == target.get("symbol")
        ),
        "source_version_consistent": (
            not opportunity.get("source_sha256")
            or opportunity.get("source_sha256") == target.get("source_sha256")
        ),
    }
    return {
        "status": "verified" if all(checks.values()) else "rejected",
        "checks": checks,
        "api_requests": 0,
        "metered_model_requests": 0,
        "main_tree_modified": False,
    }


def _failed_worker_cycle(
    result: dict[str, Any],
    coordination: Mapping[str, Any],
) -> dict[str, Any]:
    """Map a failed worker report without losing sanitized provider metadata."""
    result["status"] = "failed"
    result["outcome"] = str(
        coordination.get("outcome") or "multi_worker_failed"
    )

    raw_details = coordination.get("worker_failure_details")
    details = (
        raw_details
        if isinstance(raw_details, Mapping)
        else {}
    )
    raw_code = details.get("code")
    code_detail = (
        raw_code
        if isinstance(raw_code, Mapping)
        else None
    )

    if (
        code_detail is not None
        and code_detail.get("type") == "ProviderError"
        and isinstance(code_detail.get("code"), str)
        and code_detail.get("code")
    ):
        request_sent = code_detail.get("request_sent") is True
        request_count_raw = code_detail.get("request_count")
        request_count = (
            int(request_count_raw)
            if type(request_count_raw) is int
            and 0 <= request_count_raw <= 100
            else int(request_sent)
        )
        retry_raw = code_detail.get("retry_after")
        retry_after = (
            int(retry_raw)
            if type(retry_raw) is int
            and 0 < retry_raw <= 86400
            else None
        )
        error = {
            "type": "ProviderError",
            "code": str(code_detail["code"])[:120],
            "request_sent": request_sent,
            "request_count": request_count,
        }
        if retry_after is not None:
            error["retry_after"] = retry_after
            result["retry_after_seconds"] = retry_after
        result["error"] = error

        usage_raw = result.get("resource_usage")
        usage = (
            dict(usage_raw)
            if isinstance(usage_raw, Mapping)
            else {}
        )
        current = usage.get("metered_model_requests")
        current_count = (
            int(current)
            if type(current) is int and current >= 0
            else 0
        )
        usage["metered_model_requests"] = max(
            current_count,
            request_count,
        )
        result["resource_usage"] = usage
        return result

    result["error"] = {
        "type": "MultiWorkerError",
        "message": result["outcome"],
    }
    return result


def run_unified_improvement_cycle(
    root: Path,
    generation: int,
    *,
    target_selector: Callable[[Path], dict[str, Any]] = select_autonomous_target,
    evidence_builder: Callable[[Path, str], dict[str, Any]] = build_opportunity_evidence,
    opportunity_researcher: Callable[[Path, str], dict[str, Any]] = research_opportunity_free,
    opportunity_verifier: Callable[[Path, Mapping[str, Any], Mapping[str, Any]], dict[str, Any]] = _verify_opportunity_for_worker,
    opportunity_handoff_runner: Callable[[Path, str], dict[str, object]] = run_opportunity_writer_handoff,
    engineering_handoff_runner: Callable[..., dict[str, object]] | None = None,
    memory_cycle_runner: Callable[..., dict[str, Any]] = run_one_improvement_cycle,
    knowledge_revalidation_runner: Callable[[Path, Mapping[str, Any]], dict[str, Any]] = revalidate_knowledge_target,
    learning_goal_runner: Callable[[Path, Mapping[str, Any]], dict[str, Any]] = run_learning_goal_target,
    metered_budget_checker: Callable[..., dict[str, Any]] = _metered_budget,
    metered_attempt_recorder: Callable[..., dict[str, Any]] = _record_metered_attempt,
    code_capability_preflight: Callable[[Path], Mapping[str, Any]] | None = None,
    keep_runtime_on: bool = False,
) -> dict[str, Any]:
    """Run one scheduler-selected memory or proactive opportunity cycle.

    The selector is local-only. Opportunity targets move through the existing
    evidence -> free/public research -> writer handoff -> evaluator/promotion
    chain. Memory targets reuse the previously verified failure-improvement
    cycle, but the selected memory identity is pinned by the unified scheduler.
    """
    root = Path(root).resolve()
    production_opportunity_defaults = bool(
        opportunity_handoff_runner is run_opportunity_writer_handoff
        and opportunity_researcher is research_opportunity_free
        and target_selector is select_autonomous_target
    )
    if (
        code_capability_preflight is None
        and production_opportunity_defaults
    ):
        code_capability_preflight = _code_generation_broker_preflight
    if (
        engineering_handoff_runner is None
        and production_opportunity_defaults
    ):
        engineering_handoff_runner = run_engineering_runtime_handoff
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        raise ValueError("generation must be a positive integer")

    store = RuntimeStateStore(root)
    state, health = store._load_raw()
    if health != "ok" or state.get("generation") != generation:
        return {"status": "stale_generation", "generation": generation}
    if state.get("desired_state") != "on" or state.get("pid") not in (None, os.getpid()):
        return {"status": "ownership_not_acquired", "generation": generation}

    started_at = state.get("started_at") or utc_now()
    journal = ActiveCycleJournal(root)
    journal_cycle_id = "sc_" + uuid4().hex
    journal.start(generation=generation, cycle_id=journal_cycle_id, phase="selection")

    def checkpoint() -> None:
        current, current_health = store._load_raw()
        if current_health != "ok" or current.get("generation") != generation:
            raise RuntimeError("autonomous cycle lost runtime ownership")
        if current.get("desired_state") != "on":
            raise _StopRequested("autonomous stop requested")
        if current.get("pid") not in (None, os.getpid()):
            raise RuntimeError("autonomous cycle lost runtime ownership")
        store.save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": os.getpid(),
            "started_at": started_at,
            "heartbeat_at": utc_now(),
            "generation": generation,
        })

    checkpoint()
    reconcile_approved_access_requests(root)
    blocked_access_targets = blocked_target_ids(root)
    if target_selector is select_autonomous_target:
        selection = target_selector(root, excluded_target_ids=blocked_access_targets)
    else:
        selection = target_selector(root)
    if not isinstance(selection, Mapping):
        raise ValueError("Autonomous target selector returned invalid data")
    selection = filter_selection_for_access(selection, blocked_access_targets)
    selection_id = selection.get("selection_id")
    target = selection.get("target") if isinstance(selection.get("target"), Mapping) else None
    resumed_access_request_ids = consume_resume_for_target(root, target)
    journal.update(
        "target_selected",
        target_selection_id=selection_id,
        opportunity_id=target.get("opportunity_id") if target is not None else None,
        memory_id=target.get("memory_id") if target is not None else None,
        knowledge_key=target.get("knowledge_key") if target is not None else None,
        learning_goal_id=target.get("learning_goal_id") if target is not None else None,
    )

    # Reuse the already verified memory-improvement path, but pin its planner to
    # the exact memory selected by the unified priority scheduler.
    if target is not None and target.get("target_kind") == "memory":
        memory_id = target.get("memory_id")
        if not isinstance(memory_id, str):
            raise ValueError("Selected memory target did not contain a memory_id")
        checkpoint()
        journal.update("memory_improvement", memory_id=memory_id, target_selection_id=selection_id)

        def selected_planner(_root: Path) -> dict[str, Any]:
            return {
                "schema_version": 1,
                "kind": "unified_memory_plan",
                "plan_id": selection_id,
                "target": dict(target),
                "alternatives": selection.get("alternatives", []),
                "selection_policy": selection.get("selection_policy"),
            }

        cycle = memory_cycle_runner(
            root, generation, planner=selected_planner, keep_runtime_on=keep_runtime_on
        )
        cycle["sira_runtime_stage"] = _RUNTIME_STAGE
        cycle["target_selection_id"] = selection_id
        cycle["target_kind"] = "memory"
        cycle["selected_target"] = dict(target)
        cycle["memory_id"] = memory_id
        cycle["access_resumed_request_ids"] = resumed_access_request_ids
        cycle["access_blocked_target_count"] = len(blocked_access_targets)
        cycle["benchmark_evidence"] = (
            dict(target["benchmark_evidence"])
            if isinstance(target.get("benchmark_evidence"), Mapping)
            else None
        )
        cycle["evaluator_feedback"] = safe_record_runtime_evaluator_feedback(
            root, cycle, target
        )
        store.runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        write_json(store.runtime_dir / "last_cycle.json", cycle)
        journal.finish(status=str(cycle.get("status") or "completed"), outcome=cycle.get("outcome"))
        return cycle

    cycle_id = journal_cycle_id
    result: dict[str, Any] = {
        "schema_version": 1,
        "kind": "self_improvement_cycle",
        "sira_runtime_stage": _RUNTIME_STAGE,
        "cycle_id": cycle_id,
        "generation": generation,
        "started_at": utc_now(),
        "completed_at": None,
        "status": "running",
        "target_selection_id": selection_id,
        "target_kind": target.get("target_kind") if target is not None else None,
        "selected_target": dict(target) if target is not None else None,
        "memory_id": None,
        "knowledge_key": target.get("knowledge_key") if target is not None and target.get("target_kind") == "knowledge_revalidation" else None,
        "knowledge_revalidation": None,
        "learning_goal_id": target.get("learning_goal_id") if target is not None and target.get("target_kind") == "learning_goal" else None,
        "learning_goal_research": None,
        "opportunity_id": None,
        "evidence_id": None,
        "research_id": None,
        "handoff_id": None,
        "promotion_id": None,
        "outcome": None,
        "promotion_performed": False,
        "main_tree_modified": False,
        "retry_after_seconds": 0,
        "resource_usage": {
            "free_public_api_requests": 0,
            "metered_model_requests": 0,
        },
        "metered_budget": None,
        "worker_coordination": None,
        "access_request_id": None,
        "access_resumed_request_ids": resumed_access_request_ids,
        "benchmark_evidence": (
            dict(target["benchmark_evidence"])
            if target is not None and isinstance(target.get("benchmark_evidence"), Mapping)
            else None
        ),
        "evaluator_feedback": None,
        "access_blocked_target_count": len(blocked_access_targets),
        "error": None,
    }

    try:
        if target is None:
            if selection.get("status") == "idle_access_blocked":
                result["status"] = "idle_access_blocked"
                result["outcome"] = "waiting_owner_access"
            else:
                result["status"] = "idle_no_candidate"
                result["outcome"] = "no_candidate"
            return result
        if target.get("target_kind") == "learning_goal":
            checkpoint()
            journal.update("learning_goal_research", learning_goal_id=target.get("learning_goal_id"), target_selection_id=selection_id)
            research = learning_goal_runner(root, dict(target))
            if not isinstance(research, Mapping):
                raise ValueError("Learning goal runner returned invalid data")
            result["learning_goal_research"] = dict(research)
            result["status"] = str(research.get("status") or "completed")
            result["outcome"] = research.get("outcome") or "learning_goal_research_completed"
            requests = research.get("api_requests")
            result["resource_usage"]["free_public_api_requests"] = int(requests) if type(requests) is int and requests > 0 else 0
            result["resource_usage"]["api_requests"] = result["resource_usage"]["free_public_api_requests"]
            result["resource_usage"]["metered_model_requests"] = 0
            result["promotion_performed"] = False
            result["main_tree_modified"] = False
            return result
        if target.get("target_kind") == "knowledge_revalidation":
            checkpoint()
            journal.update("knowledge_revalidation", knowledge_key=target.get("knowledge_key"), target_selection_id=selection_id)
            revalidation = knowledge_revalidation_runner(root, dict(target))
            if not isinstance(revalidation, Mapping):
                raise ValueError("Knowledge revalidation runner returned invalid data")
            result["knowledge_revalidation"] = dict(revalidation)
            result["status"] = str(revalidation.get("status") or "completed")
            result["outcome"] = revalidation.get("outcome") or "knowledge_revalidation_completed"
            result["promotion_performed"] = False
            result["main_tree_modified"] = False
            requests = revalidation.get("api_requests")
            result["resource_usage"]["free_public_api_requests"] = int(requests) if isinstance(requests, int) and not isinstance(requests, bool) and requests > 0 else 0
            result["resource_usage"]["metered_model_requests"] = 0
            return result

        if target.get("target_kind") != "opportunity":
            raise ValueError("Autonomous target kind is unsupported")

        opportunity_id = target.get("opportunity_id")
        if not isinstance(opportunity_id, str):
            raise ValueError("Selected opportunity did not contain an opportunity_id")
        result["opportunity_id"] = opportunity_id

        checkpoint()
        journal.update("evidence", opportunity_id=opportunity_id, target_selection_id=selection_id)
        evidence = evidence_builder(root, opportunity_id)
        evidence_id = evidence.get("evidence_id") if isinstance(evidence, Mapping) else None
        if not isinstance(evidence_id, str):
            raise ValueError("Opportunity evidence did not produce an evidence_id")
        result["evidence_id"] = evidence_id
        journal.update("evidence_ready", evidence_id=evidence_id)
        assessment = evidence.get("assessment") if isinstance(evidence.get("assessment"), Mapping) else {}
        if assessment.get("decision") != "research_ready":
            result["status"] = "completed"
            result["outcome"] = "opportunity_not_research_ready"
            OpportunityStore(root).mark_attempt(dict(target), result["outcome"])
            return result

        checkpoint()
        journal.update("multi_worker_dispatch", evidence_id=evidence_id)
        coordinator = MultiWorkerCoordinator(root)

        def research_worker(_ctx, _target):
            research = opportunity_researcher(root, evidence_id)
            if not isinstance(research, Mapping):
                raise ValueError("Opportunity research returned invalid data")
            return dict(research)

        def verification_worker(_ctx, worker_target):
            verification = opportunity_verifier(root, dict(worker_target), dict(evidence))
            if not isinstance(verification, Mapping):
                raise ValueError("Opportunity verification returned invalid data")
            return dict(verification)

        def dispatch_guard() -> bool:
            current, current_health = store._load_raw()
            return bool(
                current_health == "ok"
                and current.get("generation") == generation
                and current.get("desired_state") == "on"
                and current.get("pid") in (None, os.getpid())
            )

        def budget_checker(_root):
            return metered_budget_checker(root, now_epoch=time.time())

        def code_worker(ctx, _worker_target, inputs):
            research = inputs.get("research") if isinstance(inputs.get("research"), Mapping) else {}
            verification = inputs.get("verification") if isinstance(inputs.get("verification"), Mapping) else {}
            quality = research.get("research_quality") if isinstance(research.get("research_quality"), Mapping) else {}
            if verification.get("status") != "verified":
                return {
                    "status": "verification_rejected",
                    "outcome": "opportunity_verification_rejected",
                    "promotion_performed": False,
                    "main_tree_modified": False,
                    "protected_promotion_gate": False,
                }
            access_need = access_need_from_research(research)
            if access_need is not None:
                return {
                    "status": "deferred_owner_access",
                    "outcome": "owner_access_required",
                    "promotion_performed": False,
                    "main_tree_modified": False,
                    "protected_promotion_gate": False,
                }
            if research.get("writer_handoff_allowed") is not True or quality.get("decision") != "writer_ready":
                return {
                    "status": "research_insufficient",
                    "outcome": "opportunity_research_insufficient",
                    "promotion_performed": False,
                    "main_tree_modified": False,
                    "protected_promotion_gate": False,
                }
            research_id = research.get("research_id")
            if not isinstance(research_id, str):
                raise ValueError("Opportunity research did not produce a research_id")

            model_broker: Mapping[str, Any] | None = None
            if code_capability_preflight is not None:
                checked = code_capability_preflight(root)
                if not isinstance(checked, Mapping):
                    raise ValueError("Code capability preflight returned invalid data")
                model_broker = dict(checked)
                broker_status = str(model_broker.get("status") or "provider_unavailable")
                selected_model = model_broker.get("selected_provider_id")
                if broker_status != "ready" or selected_model != "gemini":
                    broker_need = access_need_from_broker_decision(model_broker)
                    if broker_need is not None:
                        return {
                            "status": "deferred_owner_access",
                            "outcome": "owner_access_required",
                            "promotion_performed": False,
                            "main_tree_modified": False,
                            "protected_promotion_gate": False,
                            "capability_broker": dict(model_broker),
                        }
                    if broker_status in {"metered_budget_blocked", "temporarily_unavailable"}:
                        return {
                            "status": "deferred_model_resource",
                            "outcome": f"model_capability_{broker_status}",
                            "retry_after_seconds": 1,
                            "promotion_performed": False,
                            "main_tree_modified": False,
                            "protected_promotion_gate": False,
                            "capability_broker": dict(model_broker),
                        }
                    return {
                        "status": "model_capability_unavailable",
                        "outcome": f"model_capability_{broker_status}",
                        "promotion_performed": False,
                        "main_tree_modified": False,
                        "protected_promotion_gate": False,
                        "capability_broker": dict(model_broker),
                    }

            lease = PromotionLease(root)
            if not lease.acquire(ctx.task_id):
                return {
                    "status": "deferred_promotion_lease",
                    "outcome": "promotion_lease_busy",
                    "retry_after_seconds": 5,
                    "promotion_performed": False,
                    "main_tree_modified": False,
                    "protected_promotion_gate": True,
                }
            try:
                metered_attempt_recorder(root, now_epoch=time.time())
                use_engineering_route = bool(
                    engineering_handoff_runner is not None
                    and engineering_runtime_eligible(
                        root,
                        dict(_worker_target),
                        dict(evidence),
                        dict(research),
                    )
                )
                if use_engineering_route:
                    handoff = engineering_handoff_runner(
                        root,
                        dict(_worker_target),
                        dict(evidence),
                        dict(research),
                        worker_task_id=ctx.task_id,
                        runtime_guard=dispatch_guard,
                    )
                else:
                    handoff = opportunity_handoff_runner(root, research_id)
                if not isinstance(handoff, Mapping):
                    raise ValueError("Opportunity handoff returned invalid data")
                if handoff.get("status") == "stopped_by_request":
                    return {
                        "status": "stopped_by_request",
                        "outcome": "stop_requested",
                        "promotion_performed": False,
                        "main_tree_modified": False,
                        "protected_promotion_gate": True,
                        "runtime_route": handoff.get("runtime_route"),
                        "handoff": dict(handoff),
                        "capability_broker": (
                            dict(model_broker)
                            if isinstance(model_broker, Mapping)
                            else None
                        ),
                    }
                return {
                    "status": "handoff_complete",
                    "outcome": handoff.get("outcome") or handoff.get("status"),
                    "promotion_performed": bool(handoff.get("promotion_performed", False)),
                    "main_tree_modified": bool(handoff.get("main_tree_modified", False)),
                    "protected_promotion_gate": True,
                    "runtime_route": handoff.get("runtime_route"),
                    "handoff": dict(handoff),
                    "capability_broker": (
                        dict(model_broker)
                        if isinstance(model_broker, Mapping)
                        else None
                    ),
                }
            finally:
                lease.release(ctx.task_id)

        coordination = coordinator.run_task(
            dict(target),
            research_worker=research_worker,
            verification_worker=verification_worker,
            code_worker=code_worker,
            budget_checker=budget_checker,
            dispatch_guard=dispatch_guard,
        )
        result["worker_coordination"] = coordination
        journal.update("multi_worker_complete", task_id=coordination.get("task_id"), outcome=coordination.get("outcome"))

        readonly = coordination.get("readonly_results") if isinstance(coordination.get("readonly_results"), Mapping) else {}
        research = readonly.get("research") if isinstance(readonly.get("research"), Mapping) else {}
        research_id = research.get("research_id")
        if isinstance(research_id, str):
            result["research_id"] = research_id
        result["resource_usage"]["free_public_api_requests"] = int(
            research.get("api_requests")
            if isinstance(research.get("api_requests"), int) and not isinstance(research.get("api_requests"), bool)
            else 0
        )
        if isinstance(coordination.get("budget"), Mapping):
            result["metered_budget"] = dict(coordination["budget"])

        if coordination.get("status") == "abandoned_reselect" and coordination.get("outcome") == "dispatch_stopped":
            result["status"] = "stopped_by_request"
            result["outcome"] = "stop_requested"
            return result

        if coordination.get("status") == "deferred_resource_budget":
            reason = str(coordination.get("outcome") or "unavailable")
            result["status"] = "deferred_resource_budget"
            result["outcome"] = (
                "metered_retry_budget_exhausted"
                if reason == "rolling_budget_exhausted"
                else "metered_minimum_interval"
                if reason == "minimum_interval"
                else "metered_resource_deferred"
            )
            retry_after = coordination.get("retry_after_seconds")
            result["retry_after_seconds"] = int(retry_after) if isinstance(retry_after, int) and retry_after > 0 else 1
            return result

        if coordination.get("status") == "failed_worker":
            return _failed_worker_cycle(
                result,
                coordination,
            )

        code_result = coordination.get("code_result") if isinstance(coordination.get("code_result"), Mapping) else {}
        code_status = code_result.get("status")
        if coordination.get("status") == "deferred_owner_access" or code_status == "deferred_owner_access":
            broker_payload = (
                code_result.get("capability_broker")
                if isinstance(code_result.get("capability_broker"), Mapping)
                else {}
            )
            access_need = access_need_from_broker_decision(broker_payload)
            if access_need is None:
                access_need = access_need_from_research(research)
            if access_need is None:
                raise ValueError("Owner-access deferral did not contain a recognized access need")
            access_request = park_target_for_access(
                root, dict(target), access_need,
                worker_task_id=(str(coordination.get("task_id"))
                    if isinstance(coordination.get("task_id"), str) else None),
            )
            result["access_request_id"] = access_request.get("request_id")
            result["status"] = "deferred_owner_access"
            result["outcome"] = "owner_access_required"
            journal.update("owner_access_required",
                access_request_id=result["access_request_id"],
                task_id=coordination.get("task_id"))
            return result
        if code_status == "deferred_model_resource":
            result["status"] = "deferred_resource_budget"
            result["outcome"] = str(
                code_result.get("outcome") or "model_capability_deferred"
            )
            retry_after = code_result.get("retry_after_seconds")
            result["retry_after_seconds"] = (
                int(retry_after)
                if isinstance(retry_after, int)
                and not isinstance(retry_after, bool)
                and retry_after > 0
                else 1
            )
            return result
        if code_status == "model_capability_unavailable":
            result["status"] = "failed"
            result["outcome"] = str(
                code_result.get("outcome") or "model_capability_unavailable"
            )
            result["error"] = {
                "type": "CapabilityBrokerUnavailable",
                "message": result["outcome"],
            }
            return result
        if code_status == "deferred_promotion_lease":
            result["status"] = "deferred_resource_budget"
            result["outcome"] = "promotion_lease_busy"
            retry_after = code_result.get("retry_after_seconds")
            result["retry_after_seconds"] = int(retry_after) if isinstance(retry_after, int) and retry_after > 0 else 5
            return result
        if code_status == "stopped_by_request":
            result["status"] = "stopped_by_request"
            result["outcome"] = "stop_requested"
            result["promotion_performed"] = False
            result["main_tree_modified"] = False
            return result
        if code_status == "verification_rejected":
            result["status"] = "completed"
            result["outcome"] = "opportunity_verification_rejected"
            OpportunityStore(root).mark_attempt(dict(target), result["outcome"])
            return result
        if code_status == "research_insufficient":
            result["status"] = "completed"
            result["outcome"] = "opportunity_research_insufficient"
            OpportunityStore(root).mark_attempt(dict(target), result["outcome"])
            return result

        handoff = code_result.get("handoff") if isinstance(code_result.get("handoff"), Mapping) else {}
        result["handoff_id"] = handoff.get("handoff_id")
        writer_report = handoff.get("writer_report") if isinstance(handoff.get("writer_report"), Mapping) else {}
        writer_api_requests = writer_report.get("api_requests")
        result["resource_usage"]["metered_model_requests"] = (
            int(writer_api_requests)
            if isinstance(writer_api_requests, int) and not isinstance(writer_api_requests, bool) and writer_api_requests > 0
            else 0
        )
        result["outcome"] = handoff.get("outcome") or handoff.get("status") or code_result.get("outcome")
        result["promotion_performed"] = bool(handoff.get("promotion_performed", False))
        result["main_tree_modified"] = bool(handoff.get("main_tree_modified", False))
        promotion = handoff.get("promotion")
        if isinstance(promotion, Mapping):
            result["promotion_id"] = promotion.get("promotion_id")
        if handoff.get("status") == "rollback_failed":
            raise RuntimeError("autonomous opportunity rollback failed")
        result["status"] = "completed"
        return result
    except _StopRequested:
        result["status"] = "stopped_by_request"
        result["outcome"] = "stop_requested"
        return result
    except Exception as error:
        result["status"] = "failed"
        result["outcome"] = "cycle_error"
        result["error"] = {"type": type(error).__name__, "message": str(error)[:500]}
        return result
    finally:
        result["completed_at"] = utc_now()
        journal.finish(status=str(result.get("status") or "unknown"), outcome=result.get("outcome"))
        store.runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        if target is not None and target.get("target_kind") == "opportunity" and result.get("evaluator_feedback") is None:
            result["evaluator_feedback"] = safe_record_runtime_evaluator_feedback(
                root, result, target
            )
        cycle_path = store.runtime_dir / "last_cycle.json"
        write_json(cycle_path, result)
        try:
            os.chmod(cycle_path, 0o600)
        except OSError:
            pass
        current, current_health = store._load_raw()
        keep_running = (
            keep_runtime_on
            and result["status"] != "stopped_by_request"
            and current_health == "ok"
            and current.get("generation") == generation
            and current.get("desired_state") == "on"
        )
        if current_health == "ok" and current.get("generation") == generation:
            store.save({
                "desired_state": "on" if keep_running else "off",
                "worker_state": "running" if keep_running else ("error" if result["status"] == "failed" else "stopped"),
                "pid": os.getpid() if keep_running else None,
                "started_at": started_at,
                "heartbeat_at": utc_now(),
                "generation": generation,
            })


def run_autonomous_loop(
    root: Path,
    generation: int,
    *,
    cycle_interval: float = 5.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    cycle_runner: Callable[..., dict[str, Any]] = run_unified_improvement_cycle,
) -> dict[str, Any]:
    """Run persistent cycles with crash recovery and resource-aware pacing."""
    root = Path(root).resolve()
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        raise ValueError("generation must be a positive integer")
    if not isinstance(cycle_interval, (int, float)) or isinstance(cycle_interval, bool) or cycle_interval < 0:
        raise ValueError("cycle_interval must be non-negative")

    store = RuntimeStateStore(root)
    state, health = store._load_raw()
    if health != "ok" or state.get("generation") != generation:
        return {"status": "stale_generation", "generation": generation, "cycles_completed": 0}
    if state.get("desired_state") != "on" or state.get("pid") not in (None, os.getpid()):
        return {"status": "ownership_not_acquired", "generation": generation, "cycles_completed": 0}

    recovery = recover_interrupted_cycle(root, recovered_by_generation=generation)
    recovered_worker_tasks = recover_orphaned_worker_tasks(root)
    lease_recovery = recover_stale_promotion_lease(root)
    multi_worker_recovery = {
        "recovered_count": len(recovered_worker_tasks),
        "task_ids": [str(row.get("task_id")) for row in recovered_worker_tasks if row.get("task_id")],
        "promotion_lease": lease_recovery,
    }
    reliability = RuntimeReliabilityStore(root)
    started_at = state.get("started_at") or utc_now()
    store.save({
        "desired_state": "on",
        "worker_state": "running",
        "pid": os.getpid(),
        "started_at": started_at,
        "heartbeat_at": utc_now(),
        "generation": generation,
    })
    cycles_completed = 0
    last_reliability = None

    def pump_notifications_safely() -> None:
        try:
            pump_owner_notifications(root)
        except Exception:
            # Owner notification transport must never stop autonomous work.
            pass

    def sleep_interruptibly(seconds: float) -> bool:
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            current, current_health = store._load_raw()
            if (current_health != "ok" or current.get("generation") != generation
                    or current.get("desired_state") != "on"):
                return False
            step = min(0.5, remaining)
            sleep_fn(step)
            remaining -= step
        return True

    try:
        pump_notifications_safely()
        while True:
            current, current_health = store._load_raw()
            if current_health != "ok" or current.get("generation") != generation:
                return {
                    "status": "stale_generation", "generation": generation,
                    "cycles_completed": cycles_completed, "recovery": recovery,
                    "multi_worker_recovery": multi_worker_recovery,
                    "last_reliability": last_reliability,
                }
            if current.get("desired_state") != "on":
                break

            persisted_delay = reliability.next_cycle_delay(now_epoch=time.time())
            if persisted_delay > 0 and not sleep_interruptibly(persisted_delay):
                break

            current, current_health = store._load_raw()
            if (current_health != "ok" or current.get("generation") != generation
                    or current.get("desired_state") != "on"):
                break

            cycle = cycle_runner(root, generation, keep_runtime_on=True)
            cycles_completed += 1
            pump_notifications_safely()
            cycle_status = cycle.get("status")
            last_reliability = reliability.record_cycle(cycle, now_epoch=time.time())
            cycle["runtime_reliability"] = last_reliability
            try:
                write_json(store.runtime_dir / "last_cycle.json", cycle)
            except OSError:
                pass

            if cycle_status in {"stopped_by_request", "stale_generation", "ownership_not_acquired"}:
                break
            if cycle_status == "failed" and last_reliability.get("failure_class") == "internal":
                return {
                    "status": "worker_error", "generation": generation,
                    "cycles_completed": cycles_completed, "recovery": recovery,
                    "multi_worker_recovery": multi_worker_recovery,
                    "last_reliability": last_reliability,
                }

            current, current_health = store._load_raw()
            if (current_health != "ok" or current.get("generation") != generation
                    or current.get("desired_state") != "on"):
                break
            delay = max(float(cycle_interval), float(last_reliability.get("delay_seconds") or 0))
            if not sleep_interruptibly(delay):
                break
        return {
            "status": "stopped",
            "generation": generation,
            "cycles_completed": cycles_completed,
            "recovery": recovery,
            "multi_worker_recovery": multi_worker_recovery,
            "last_reliability": last_reliability,
        }
    finally:
        current, current_health = store._load_raw()
        if current_health == "ok" and current.get("generation") == generation:
            store.save({
                "desired_state": "off",
                "worker_state": "stopped",
                "pid": None,
                "started_at": started_at,
                "heartbeat_at": utc_now(),
                "generation": generation,
            })


class AutonomousRuntime:
    """Start/stop the persistent autonomous improvement worker."""

    def __init__(self, root: Path, *, launcher: WorkerLauncher | None = None):
        self.root = Path(root).resolve()
        self.store = RuntimeStateStore(self.root)
        self.launcher = launcher or SubprocessWorkerLauncher()

    def start(self) -> dict[str, Any]:
        status = self.store.status()
        recovered_from_stale = status.get("state_health") == "stale_worker"
        if (status["effective_state"] == "running"
                or (status["desired_state"] == "on" and status["worker_alive"]
                    and status["worker_state"] in {"starting", "running"})):
            return {
                "status": "already_running",
                "sira_runtime_stage": _RUNTIME_STAGE,
                "generation": status["generation"],
                "pid": status["pid"],
                "mode": "persistent_improvement_loop",
                "recovered_from_stale": False,
            }

        generation = int(status.get("generation") or 0) + 1
        started_at = utc_now()
        self.store.save({
            "desired_state": "on",
            "worker_state": "starting",
            "pid": None,
            "started_at": started_at,
            "heartbeat_at": started_at,
            "generation": generation,
        })
        try:
            process = self.launcher.launch(self.root, generation)
            pid = int(process.pid)
            if pid <= 0:
                raise OSError("worker launcher returned invalid pid")
        except Exception:
            self.store.save({
                "desired_state": "off",
                "worker_state": "error",
                "pid": None,
                "started_at": started_at,
                "heartbeat_at": utc_now(),
                "generation": generation,
            })
            raise

        self.store.save({
            "desired_state": "on",
            "worker_state": "starting",
            "pid": pid,
            "started_at": started_at,
            "heartbeat_at": utc_now(),
            "generation": generation,
        })
        return {
            "status": "started",
            "sira_runtime_stage": _RUNTIME_STAGE,
            "generation": generation,
            "pid": pid,
            "mode": "persistent_improvement_loop",
            "recovered_from_stale": recovered_from_stale,
        }

    def stop(self) -> dict[str, Any]:
        status = self.store.status()
        generation = int(status.get("generation") or 0)
        if status.get("state_health") == "stale_worker":
            self.store.save({
                "desired_state": "off",
                "worker_state": "stopped",
                "pid": None,
                "started_at": status.get("started_at"),
                "heartbeat_at": utc_now(),
                "generation": generation,
            })
            return {
                "status": "stale_worker_cleared",
                "sira_runtime_stage": _RUNTIME_STAGE,
                "generation": generation,
                "pid": None,
                "mode": "persistent_improvement_loop",
            }
        if not status.get("worker_alive") or status.get("desired_state") == "off":
            return {
                "status": "already_stopped",
                "sira_runtime_stage": _RUNTIME_STAGE,
                "generation": generation,
                "pid": None,
                "mode": "persistent_improvement_loop",
            }
        self.store.save({
            "desired_state": "off",
            "worker_state": "stopping",
            "pid": status.get("pid"),
            "started_at": status.get("started_at"),
            "heartbeat_at": utc_now(),
            "generation": generation,
        })
        return {
            "status": "stop_requested",
            "sira_runtime_stage": _RUNTIME_STAGE,
            "generation": generation,
            "pid": status.get("pid"),
            "mode": "persistent_improvement_loop",
        }
