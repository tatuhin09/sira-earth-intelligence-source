"""Bounded multi-worker coordination primitives for autonomous improvement.

This module intentionally stops short of promotion. It provides isolated worker
artifacts, a shared task record, bounded parallel read-only work, serialized
code work behind the existing global metered budget, crash-safe task recovery,
and a single-owner promotion lease for later runtime integration.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4

from .models import ProviderError, utc_now
from .storage import write_json

WORKER_COORDINATION_POLICY_VERSION = 1
MAX_PARALLEL_READONLY_WORKERS = 2
_ALLOWED_TASK_STATES = {
    "queued",
    "running",
    "completed_readonly",
    "candidate_prepared",
    "deferred_resource_budget",
    "deferred_owner_access",
    "failed_worker",
    "abandoned_reselect",
}


@dataclass(frozen=True)
class WorkerContext:
    project_root: Path
    task_id: str
    role: str
    workspace: Path
    artifact_dir: Path


class WorkerTaskStore:
    """Persist one structured task record per coordinated improvement target."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.base = self.root / "improvements" / "workers"
        self.tasks_dir = self.base / "tasks"
        self.work_dir = self.base / "work"
        self._lock = threading.RLock()

    def _path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not task_id.startswith("mw_"):
            raise ValueError("invalid multi-worker task id")
        return self.tasks_dir / f"{task_id}.json"

    def create(self, target: Mapping[str, Any], *, roles: tuple[str, ...]) -> dict[str, Any]:
        if not isinstance(target, Mapping) or not target:
            raise ValueError("multi-worker target must be a non-empty mapping")
        if not roles or len(set(roles)) != len(roles):
            raise ValueError("worker roles must be unique and non-empty")
        task_id = "mw_" + uuid4().hex
        task_root = self.work_dir / task_id
        payload = {
            "schema_version": 1,
            "kind": "multi_worker_task",
            "policy_version": WORKER_COORDINATION_POLICY_VERSION,
            "task_id": task_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "status": "queued",
            "owner_pid": os.getpid(),
            "heartbeat_at": utc_now(),
            "target": dict(target),
            "task_root": str(task_root),
            "workers": {
                role: {
                    "status": "queued",
                    "owner_pid": None,
                    "heartbeat_at": None,
                    "workspace": str(task_root / "workspaces" / role),
                    "artifact_dir": str(task_root / "artifacts" / role),
                    "error": None,
                }
                for role in roles
            },
            "promotion_performed": False,
        }
        write_json(self._path(task_id), payload)
        return payload

    def load(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                payload = json.loads(self._path(task_id).read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("multi-worker task record is unavailable") from exc
            if not isinstance(payload, dict) or payload.get("kind") != "multi_worker_task":
                raise ValueError("invalid multi-worker task record")
            return payload

    def update(self, task_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            payload = self.load(task_id)
            if "status" in fields and fields["status"] not in _ALLOWED_TASK_STATES:
                raise ValueError("invalid multi-worker task status")
            for key, value in fields.items():
                if key == "workers":
                    raise ValueError("workers must be updated through update_worker")
                payload[key] = value
            payload["updated_at"] = utc_now()
            payload["heartbeat_at"] = payload["updated_at"]
            write_json(self._path(task_id), payload)
            return payload

    def update_worker(self, task_id: str, role: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            payload = self.load(task_id)
            workers = payload.get("workers")
            if not isinstance(workers, dict) or role not in workers:
                raise ValueError("unknown worker role")
            current = dict(workers[role])
            current.update(fields)
            workers[role] = current
            payload["workers"] = workers
            payload["updated_at"] = utc_now()
            payload["heartbeat_at"] = payload["updated_at"]
            write_json(self._path(task_id), payload)
            return payload

    def iter_records(self):
        if not self.tasks_dir.is_dir():
            return
        for path in sorted(self.tasks_dir.glob("mw_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("kind") == "multi_worker_task":
                yield payload


class PromotionLease:
    """Small local single-owner lease; promotion code remains the authority."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = self.root / "runtime" / "promotion_worker_lease.json"

    def acquire(self, owner_id: str) -> bool:
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("promotion lease owner is required")
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        payload = json.dumps({
            "schema_version": 1,
            "kind": "promotion_worker_lease",
            "owner_id": owner_id,
            "owner_pid": os.getpid(),
            "created_at": utc_now(),
        }, ensure_ascii=True).encode("utf-8")
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        return True

    def _owner(self) -> str | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload.get("owner_id") if isinstance(payload, dict) else None

    def release(self, owner_id: str) -> bool:
        if self._owner() != owner_id:
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


def _process_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def recover_stale_promotion_lease(root: Path) -> dict[str, Any]:
    """Remove only a lease whose recorded owner process is confirmed dead."""
    lease = PromotionLease(root)
    try:
        payload = json.loads(lease.path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"recovered": False, "reason": "missing"}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"recovered": False, "reason": "invalid_fail_closed"}
    if not isinstance(payload, dict) or payload.get("kind") != "promotion_worker_lease":
        return {"recovered": False, "reason": "invalid_fail_closed"}
    owner_pid = payload.get("owner_pid")
    if not isinstance(owner_pid, int) or isinstance(owner_pid, bool) or owner_pid <= 0:
        return {"recovered": False, "reason": "unknown_owner_fail_closed"}
    if _process_alive(owner_pid):
        return {"recovered": False, "reason": "live_owner", "owner_pid": owner_pid}
    try:
        lease.path.unlink()
    except FileNotFoundError:
        return {"recovered": False, "reason": "missing"}
    return {
        "recovered": True,
        "reason": "dead_owner",
        "owner_id": payload.get("owner_id"),
        "owner_pid": owner_pid,
    }


class MultiWorkerCoordinator:
    """Coordinate bounded parallel read-only work and one serialized code phase."""

    def __init__(self, root: Path, *, max_readonly_workers: int = MAX_PARALLEL_READONLY_WORKERS):
        self.root = Path(root).resolve()
        if not isinstance(max_readonly_workers, int) or isinstance(max_readonly_workers, bool):
            raise ValueError("max_readonly_workers must be an integer")
        if not 1 <= max_readonly_workers <= MAX_PARALLEL_READONLY_WORKERS:
            raise ValueError(f"max_readonly_workers must be between 1 and {MAX_PARALLEL_READONLY_WORKERS}")
        self.max_readonly_workers = max_readonly_workers
        self.store = WorkerTaskStore(self.root)

    def _context(self, task: Mapping[str, Any], role: str) -> WorkerContext:
        task_root = Path(str(task["task_root"]))
        workspace = task_root / "workspaces" / role
        artifact_dir = task_root / "artifacts" / role
        workspace.mkdir(parents=True, mode=0o700, exist_ok=True)
        artifact_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        return WorkerContext(
            project_root=self.root,
            task_id=str(task["task_id"]),
            role=role,
            workspace=workspace,
            artifact_dir=artifact_dir,
        )

    @staticmethod
    def _normalize_result(value: object) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("worker result must be a mapping")
        return dict(value)

    def _run_readonly_worker(
        self,
        task: Mapping[str, Any],
        role: str,
        worker: Callable[[WorkerContext, Mapping[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        ctx = self._context(task, role)
        self.store.update_worker(
            str(task["task_id"]), role, status="running", owner_pid=os.getpid(),
            started_at=utc_now(), heartbeat_at=utc_now(),
        )
        try:
            result = self._normalize_result(worker(ctx, dict(task["target"])))
        except Exception as exc:
            self.store.update_worker(
                str(task["task_id"]), role, status="failed", error=type(exc).__name__,
                heartbeat_at=utc_now(), completed_at=utc_now()
            )
            raise
        write_json(ctx.artifact_dir / "result.json", result)
        self.store.update_worker(
            str(task["task_id"]), role, status="completed", error=None, heartbeat_at=utc_now(),
            completed_at=utc_now(), result_artifact=str(ctx.artifact_dir / "result.json"),
        )
        return result

    def run_task(
        self,
        target: Mapping[str, Any],
        *,
        research_worker: Callable[[WorkerContext, Mapping[str, Any]], Mapping[str, Any]],
        verification_worker: Callable[[WorkerContext, Mapping[str, Any]], Mapping[str, Any]],
        code_worker: Callable[[WorkerContext, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
        budget_checker: Callable[[Path], Mapping[str, Any]] | None = None,
        dispatch_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        roles = ("research", "verification") + (("code",) if code_worker is not None else ())
        task = self.store.create(target, roles=roles)
        task_id = str(task["task_id"])
        self.store.update(task_id, status="running", started_at=utc_now())
        readonly_results: dict[str, dict[str, Any]] = {}
        failures: dict[str, str] = {}
        functions = {"research": research_worker, "verification": verification_worker}

        with ThreadPoolExecutor(max_workers=self.max_readonly_workers, thread_name_prefix="sira-worker") as executor:
            future_roles = {
                executor.submit(self._run_readonly_worker, task, role, worker): role
                for role, worker in functions.items()
            }
            for future in as_completed(future_roles):
                role = future_roles[future]
                try:
                    readonly_results[role] = future.result()
                except Exception as exc:
                    failures[role] = type(exc).__name__

        self.store.update(task_id, readonly_results=readonly_results)

        if failures:
            final = self.store.update(
                task_id,
                status="failed_worker",
                outcome="readonly_worker_failed",
                worker_failures=failures,
                completed_at=utc_now(),
            )
            return self._report(final)

        if code_worker is None:
            final = self.store.update(
                task_id,
                status="completed_readonly",
                outcome="readonly_phase_completed",
                completed_at=utc_now(),
            )
            return self._report(final)

        if dispatch_guard is not None and not bool(dispatch_guard()):
            self.store.update_worker(
                task_id, "code", status="cancelled", error=None, heartbeat_at=utc_now(), completed_at=utc_now()
            )
            final = self.store.update(
                task_id, status="abandoned_reselect", outcome="dispatch_stopped", completed_at=utc_now()
            )
            return self._report(final)

        if budget_checker is not None:
            budget = dict(budget_checker(self.root))
            self.store.update(task_id, budget=budget)
            if not bool(budget.get("allowed")):
                retry_after = max(0, int(budget.get("retry_after_seconds") or 0))
                self.store.update_worker(
                    task_id, "code", status="deferred", error=None, heartbeat_at=utc_now(), completed_at=utc_now()
                )
                final = self.store.update(
                    task_id,
                    status="deferred_resource_budget",
                    outcome=str(budget.get("reason") or "resource_budget_closed"),
                    retry_after_seconds=retry_after,
                    completed_at=utc_now(),
                )
                return self._report(final)

        code_ctx = self._context(task, "code")
        self.store.update_worker(
            task_id, "code", status="running", owner_pid=os.getpid(),
            started_at=utc_now(), heartbeat_at=utc_now(),
        )
        try:
            code_result = self._normalize_result(code_worker(code_ctx, dict(task["target"]), readonly_results))
        except ProviderError as exc:
            retry_after = (
                int(exc.retry_after)
                if type(exc.retry_after) is int
                and 0 < exc.retry_after <= 86400
                else None
            )
            request_count = (
                int(exc.request_count)
                if type(exc.request_count) is int
                and 0 <= exc.request_count <= 100
                else int(bool(exc.request_sent))
            )
            detail = {
                "type": "ProviderError",
                "code": str(exc.code)[:120],
                "request_sent": bool(exc.request_sent),
                "retry_after": retry_after,
                "request_count": request_count,
            }
            self.store.update_worker(
                task_id,
                "code",
                status="failed",
                error="ProviderError",
                heartbeat_at=utc_now(),
                completed_at=utc_now(),
            )
            final = self.store.update(
                task_id,
                status="failed_worker",
                outcome="code_worker_failed",
                retry_after_seconds=retry_after or 0,
                worker_failures={"code": "ProviderError"},
                worker_failure_details={"code": detail},
                completed_at=utc_now(),
            )
            return self._report(final)
        except Exception as exc:
            self.store.update_worker(
                task_id, "code", status="failed", error=type(exc).__name__,
                heartbeat_at=utc_now(), completed_at=utc_now()
            )
            final = self.store.update(
                task_id,
                status="failed_worker",
                outcome="code_worker_failed",
                worker_failures={"code": type(exc).__name__},
                completed_at=utc_now(),
            )
            return self._report(final)
        if code_result.get("status") == "deferred_owner_access":
            write_json(code_ctx.artifact_dir / "result.json", code_result)
            self.store.update_worker(
                task_id, "code", status="deferred", error=None,
                heartbeat_at=utc_now(), completed_at=utc_now(),
                result_artifact=str(code_ctx.artifact_dir / "result.json"),
            )
            final = self.store.update(
                task_id, status="deferred_owner_access",
                outcome=str(code_result.get("outcome") or "owner_access_required"),
                code_result=code_result, promotion_performed=False,
                completed_at=utc_now(),
            )
            return self._report(final)

        write_json(code_ctx.artifact_dir / "result.json", code_result)
        self.store.update_worker(
            task_id,
            "code",
            status="completed",
            error=None,
            heartbeat_at=utc_now(),
            completed_at=utc_now(),
            result_artifact=str(code_ctx.artifact_dir / "result.json"),
        )
        final = self.store.update(
            task_id,
            status="candidate_prepared",
            outcome=str(code_result.get("status") or "candidate_prepared"),
            code_result=code_result,
            promotion_performed=bool(code_result.get("promotion_performed")),
            completed_at=utc_now(),
        )
        return self._report(final)

    @staticmethod
    def _report(task: Mapping[str, Any]) -> dict[str, Any]:
        workers = task.get("workers") if isinstance(task.get("workers"), Mapping) else {}
        return {
            "schema_version": 1,
            "kind": "multi_worker_coordination_report",
            "policy_version": WORKER_COORDINATION_POLICY_VERSION,
            "task_id": task.get("task_id"),
            "status": task.get("status"),
            "outcome": task.get("outcome"),
            "retry_after_seconds": task.get("retry_after_seconds", 0),
            "worker_states": {
                role: value.get("status")
                for role, value in workers.items()
                if isinstance(value, Mapping)
            },
            "promotion_performed": bool(task.get("promotion_performed")),
            "protected_promotion_gate": bool(
                isinstance(task.get("code_result"), Mapping)
                and task.get("code_result", {}).get("protected_promotion_gate")
            ),
            "owner_pid": task.get("owner_pid"),
            "heartbeat_at": task.get("heartbeat_at"),
            "readonly_results": task.get("readonly_results") if isinstance(task.get("readonly_results"), Mapping) else {},
            "code_result": task.get("code_result") if isinstance(task.get("code_result"), Mapping) else None,
            "worker_failures": task.get("worker_failures") if isinstance(task.get("worker_failures"), Mapping) else {},
            "worker_failure_details": task.get("worker_failure_details") if isinstance(task.get("worker_failure_details"), Mapping) else {},
            "budget": task.get("budget") if isinstance(task.get("budget"), Mapping) else None,
            "task_root": task.get("task_root"),
            "task_record": str(Path(str(task.get("task_root"))).parents[1] / "tasks" / f"{task.get('task_id')}.json"),
        }


def recover_orphaned_worker_tasks(root: Path) -> list[dict[str, Any]]:
    """Fail closed: abandon interrupted worker tasks and force fresh reselection."""
    store = WorkerTaskStore(root)
    recovered: list[dict[str, Any]] = []
    for task in list(store.iter_records() or ()):
        if task.get("status") != "running":
            continue
        updated = store.update(
            str(task["task_id"]),
            status="abandoned_reselect",
            outcome="interrupted_worker_task",
            recovered_at=utc_now(),
            promotion_performed=False,
        )
        recovered.append(updated)
    return recovered


def run_multi_worker_check(root: Path) -> dict[str, Any]:
    """Offline diagnostic proving isolation, concurrency, serialization and lease behavior."""
    root = Path(root).resolve()
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="sira-multi-worker-check-") as tmp:
        check_root = Path(tmp)
        coordinator = MultiWorkerCoordinator(check_root)
        barrier = __import__("threading").Barrier(2)
        workspaces: dict[str, str] = {}

        def readonly(ctx: WorkerContext, _target: Mapping[str, Any]) -> Mapping[str, Any]:
            workspaces[ctx.role] = str(ctx.workspace)
            barrier.wait(timeout=2)
            return {"status": "ok", "api_requests": 0}

        report = coordinator.run_task(
            {"target_kind": "diagnostic", "id": "multi-worker-check"},
            research_worker=readonly,
            verification_worker=readonly,
        )
        checks["parallel_readonly_completed"] = report.get("status") == "completed_readonly"
        checks["isolated_workspaces"] = len(set(workspaces.values())) == 2

        lease_a = PromotionLease(check_root)
        lease_b = PromotionLease(check_root)
        first = lease_a.acquire("diagnostic-a")
        second = lease_b.acquire("diagnostic-b")
        released = lease_a.release("diagnostic-a")
        third = lease_b.acquire("diagnostic-b")
        lease_b.release("diagnostic-b")
        checks["single_promotion_lease"] = first and not second and released and third

        store = WorkerTaskStore(check_root)
        task = store.create({"target_kind": "diagnostic", "id": "recovery"}, roles=("research", "verification"))
        store.update(task["task_id"], status="running")
        recovered = recover_orphaned_worker_tasks(check_root)
        checks["orphan_recovery_reselects"] = bool(recovered and recovered[0].get("status") == "abandoned_reselect")

    status = "passed" if all(checks.values()) else "failed"
    result = {
        "schema_version": 1,
        "kind": "multi_worker_foundation_check",
        "policy_version": WORKER_COORDINATION_POLICY_VERSION,
        "created_at": utc_now(),
        "status": status,
        "checks": checks,
        "api_requests": 0,
        "metered_model_requests": 0,
        "paid_spending": False,
    }
    artifact = root / "runtime" / "checks" / f"multi_worker_{uuid4().hex}.json"
    result["artifact"] = str(artifact)
    write_json(artifact, result)
    return result
