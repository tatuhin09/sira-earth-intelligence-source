"""Persistent resource pacing and crash-safe cycle journaling for SIRA."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping
from uuid import uuid4

from .models import utc_now
from .storage import write_json

RELIABILITY_POLICY_VERSION = 1
METERED_MIN_INTERVAL_SECONDS = 120
METERED_WINDOW_SECONDS = 15 * 60
METERED_ATTEMPT_LIMIT = 3
MAX_METERED_HISTORY = 32
IDLE_DELAY_SECONDS = 30
NORMAL_DELAY_SECONDS = 15
TRANSIENT_BACKOFF_BASE_SECONDS = 30
TRANSIENT_BACKOFF_MAX_SECONDS = 15 * 60
CONFIGURATION_COOLDOWN_SECONDS = 15 * 60
INTERNAL_FAILURE_COOLDOWN_SECONDS = 60

_TRANSIENT_CODES = {
    "http_429", "http_502", "http_503", "http_504", "timeout", "network_error",
    "connection_error", "temporarily_unavailable", "provider_unavailable",
}
_CONFIGURATION_CODES = {
    "http_401", "http_403", "missing_model_key", "missing_api_key", "invalid_api_key",
    "configuration_error", "credential_error", "authentication_error",
}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def _safe_nonnegative(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return float(default)


def _resource_usage(cycle: Mapping[str, Any]) -> Mapping[str, Any]:
    value = cycle.get("resource_usage")
    return value if isinstance(value, Mapping) else {}


def classify_cycle(cycle: Mapping[str, Any]) -> str:
    """Classify a completed cycle without trusting provider text or HTTP bodies."""
    outcome = str(cycle.get("outcome") or "").strip().lower()
    status = str(cycle.get("status") or "").strip().lower()
    error = cycle.get("error") if isinstance(cycle.get("error"), Mapping) else {}
    error_code = str(error.get("code") or "").strip().lower()
    error_type = str(error.get("type") or "").strip().lower()
    tokens = {outcome, error_code}

    if any(token in _TRANSIENT_CODES or token.startswith("http_5") for token in tokens if token):
        return "transient_external"
    if any(token in _CONFIGURATION_CODES for token in tokens if token):
        return "configuration"
    if any(word in outcome for word in ("missing_key", "credential", "authentication", "configuration")):
        return "configuration"
    if "providererror" in error_type and status == "failed":
        return "transient_external"
    if status == "failed":
        return "internal"
    return "none"


class RuntimeReliabilityStore:
    """Bounded persistent scheduler state; contains no credentials or provider payloads."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.runtime_dir = self.root / "runtime"
        self.path = self.runtime_dir / "reliability.json"

    @staticmethod
    def _default() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "policy_version": RELIABILITY_POLICY_VERSION,
            "metered_attempts": [],
            "transient_streak": 0,
            "next_cycle_not_before_epoch": 0.0,
            "last_failure_class": "none",
            "last_outcome": None,
            "updated_at": None,
        }

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._default()
        except (OSError, UnicodeError, json.JSONDecodeError):
            # Corrupt pacing state must not authorize extra metered calls. Fail
            # closed for one configuration cooldown while remaining recoverable.
            state = self._default()
            state["next_cycle_not_before_epoch"] = time.time() + CONFIGURATION_COOLDOWN_SECONDS
            state["last_failure_class"] = "configuration"
            return state
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            return self._default()
        attempts = raw.get("metered_attempts")
        if not isinstance(attempts, list):
            attempts = []
        clean_attempts = [
            float(value) for value in attempts[-MAX_METERED_HISTORY:]
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
        ]
        streak = raw.get("transient_streak")
        if not isinstance(streak, int) or isinstance(streak, bool) or streak < 0:
            streak = 0
        return {
            "schema_version": 1,
            "policy_version": RELIABILITY_POLICY_VERSION,
            "metered_attempts": clean_attempts,
            "transient_streak": min(streak, 20),
            "next_cycle_not_before_epoch": _safe_nonnegative(raw.get("next_cycle_not_before_epoch")),
            "last_failure_class": str(raw.get("last_failure_class") or "none")[:80],
            "last_outcome": str(raw.get("last_outcome"))[:120] if raw.get("last_outcome") is not None else None,
            "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
        }

    def _save(self, state: Mapping[str, Any]) -> None:
        self.runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        payload = dict(state)
        payload["schema_version"] = 1
        payload["policy_version"] = RELIABILITY_POLICY_VERSION
        payload["updated_at"] = utc_now()
        write_json(self.path, payload)

    def metered_budget(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        now = time.time() if now_epoch is None else float(now_epoch)
        state = self._load()
        cutoff = now - METERED_WINDOW_SECONDS
        attempts = sorted(value for value in state["metered_attempts"] if value >= cutoff and value <= now)
        retry_after = 0
        reason = "available"
        allowed = True
        if len(attempts) >= METERED_ATTEMPT_LIMIT:
            allowed = False
            reason = "rolling_budget_exhausted"
            retry_after = max(1, int((attempts[0] + METERED_WINDOW_SECONDS) - now))
        elif attempts and now - attempts[-1] < METERED_MIN_INTERVAL_SECONDS:
            allowed = False
            reason = "minimum_interval"
            retry_after = max(1, int(METERED_MIN_INTERVAL_SECONDS - (now - attempts[-1])))
        return {
            "allowed": allowed,
            "reason": reason,
            "retry_after_seconds": retry_after,
            "attempts_in_window": len(attempts),
            "attempt_limit": METERED_ATTEMPT_LIMIT,
            "window_seconds": METERED_WINDOW_SECONDS,
            "minimum_interval_seconds": METERED_MIN_INTERVAL_SECONDS,
        }

    def record_metered_attempt(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        now = time.time() if now_epoch is None else float(now_epoch)
        state = self._load()
        cutoff = now - METERED_WINDOW_SECONDS
        attempts = [value for value in state["metered_attempts"] if value >= cutoff and value <= now]
        attempts.append(now)
        state["metered_attempts"] = attempts[-MAX_METERED_HISTORY:]
        self._save(state)
        return {"recorded": True, "attempted_at_epoch": now, "attempted_at": _iso(now)}

    def record_cycle(self, cycle: Mapping[str, Any], *, now_epoch: float | None = None) -> dict[str, Any]:
        now = time.time() if now_epoch is None else float(now_epoch)
        state = self._load()
        failure_class = classify_cycle(cycle)
        if failure_class == "transient_external":
            streak = min(int(state.get("transient_streak") or 0) + 1, 20)
            delay = min(TRANSIENT_BACKOFF_MAX_SECONDS, TRANSIENT_BACKOFF_BASE_SECONDS * (2 ** (streak - 1)))
        elif failure_class == "configuration":
            streak = 0
            delay = CONFIGURATION_COOLDOWN_SECONDS
        elif failure_class == "internal":
            streak = 0
            delay = INTERNAL_FAILURE_COOLDOWN_SECONDS
        else:
            streak = 0
            status = str(cycle.get("status") or "")
            if status == "idle_no_candidate":
                delay = IDLE_DELAY_SECONDS
            elif status == "deferred_resource_budget":
                delay = max(1, int(_safe_nonnegative(cycle.get("retry_after_seconds"), NORMAL_DELAY_SECONDS)))
            else:
                metered = int(_safe_nonnegative(_resource_usage(cycle).get("metered_model_requests")))
                delay = max(NORMAL_DELAY_SECONDS, METERED_MIN_INTERVAL_SECONDS if metered else 0)
        state["transient_streak"] = streak
        state["next_cycle_not_before_epoch"] = now + float(delay)
        state["last_failure_class"] = failure_class
        state["last_outcome"] = str(cycle.get("outcome"))[:120] if cycle.get("outcome") is not None else None
        self._save(state)
        return {
            "failure_class": failure_class,
            "transient_streak": streak,
            "delay_seconds": int(delay),
            "next_cycle_not_before_epoch": now + float(delay),
            "next_cycle_not_before": _iso(now + float(delay)),
            "worker_should_continue": failure_class != "internal",
        }

    def next_cycle_delay(self, *, now_epoch: float | None = None) -> int:
        now = time.time() if now_epoch is None else float(now_epoch)
        state = self._load()
        return max(0, int(state["next_cycle_not_before_epoch"] - now))

    def status(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        now = time.time() if now_epoch is None else float(now_epoch)
        state = self._load()
        budget = self.metered_budget(now_epoch=now)
        return {
            "policy_version": RELIABILITY_POLICY_VERSION,
            "transient_streak": state["transient_streak"],
            "next_cycle_in_seconds": self.next_cycle_delay(now_epoch=now),
            "last_failure_class": state["last_failure_class"],
            "last_outcome": state["last_outcome"],
            "metered_budget": budget,
            "state_file": str(self.path),
        }


class ActiveCycleJournal:
    """One atomic marker used only to detect a process killed mid-cycle."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.runtime_dir = self.root / "runtime"
        self.path = self.runtime_dir / "active_cycle.json"

    def start(self, *, generation: int, cycle_id: str, phase: str) -> dict[str, Any]:
        self.runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        payload = {
            "schema_version": 1,
            "kind": "active_autonomous_cycle",
            "cycle_id": cycle_id,
            "generation": int(generation),
            "phase": str(phase),
            "started_at": utc_now(),
            "updated_at": utc_now(),
        }
        write_json(self.path, payload)
        return payload

    def update(self, phase: str, **fields: Any) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("active cycle journal is unavailable") from exc
        if not isinstance(payload, dict) or payload.get("kind") != "active_autonomous_cycle":
            raise RuntimeError("active cycle journal is invalid")
        payload["phase"] = str(phase)
        for key, value in fields.items():
            if key in {"opportunity_id", "memory_id", "evidence_id", "research_id", "handoff_id", "target_selection_id"}:
                payload[key] = value
        payload["updated_at"] = utc_now()
        write_json(self.path, payload)
        return payload

    def finish(self, *, status: str, outcome: object) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return


def recover_interrupted_cycle(root: Path, *, recovered_by_generation: int) -> dict[str, Any]:
    root = Path(root).resolve()
    journal = ActiveCycleJournal(root)
    try:
        raw = json.loads(journal.path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "nothing_to_recover", "recovered_by_generation": recovered_by_generation}
    except (OSError, UnicodeError, json.JSONDecodeError):
        raw = {"cycle_id": None, "generation": None, "phase": "unknown_corrupt"}
    if not isinstance(raw, dict):
        raw = {"cycle_id": None, "generation": None, "phase": "unknown_corrupt"}
    recovery_id = "rr_" + uuid4().hex
    recovery_dir = root / "runtime" / "recovery"
    artifact = recovery_dir / f"{recovery_id}.json"
    report = {
        "schema_version": 1,
        "kind": "autonomous_cycle_recovery",
        "recovery_id": recovery_id,
        "created_at": utc_now(),
        "status": "recovered_interrupted_cycle",
        "action": "abandoned_and_reselect",
        "interrupted_cycle_id": raw.get("cycle_id"),
        "interrupted_generation": raw.get("generation"),
        "interrupted_phase": raw.get("phase"),
        "recovered_by_generation": recovered_by_generation,
        "artifact": str(artifact),
    }
    write_json(artifact, report)
    write_json(root / "runtime" / "last_recovery.json", report)
    try:
        journal.path.unlink()
    except FileNotFoundError:
        pass
    return report


def run_reliability_check(root: Path) -> dict[str, Any]:
    """Offline diagnostic for pacing, budget, and interrupted-cycle recovery."""
    root = Path(root).resolve()
    with tempfile.TemporaryDirectory(prefix="sira-reliability-check-") as tmp:
        check_root = Path(tmp)
        store = RuntimeReliabilityStore(check_root)
        checks: dict[str, bool] = {}
        checks["initial_budget_open"] = bool(store.metered_budget(now_epoch=1000.0)["allowed"])
        store.record_metered_attempt(now_epoch=1000.0)
        checks["minimum_interval_blocks"] = store.metered_budget(now_epoch=1050.0)["reason"] == "minimum_interval"
        first = store.record_cycle({"status": "completed", "outcome": "http_503"}, now_epoch=1100.0)
        second = store.record_cycle({"status": "completed", "outcome": "http_503"}, now_epoch=1130.0)
        checks["transient_backoff_grows"] = second["delay_seconds"] > first["delay_seconds"]
        journal = ActiveCycleJournal(check_root)
        journal.start(generation=1, cycle_id="sc_" + "c" * 32, phase="writer_handoff")
        recovery = recover_interrupted_cycle(check_root, recovered_by_generation=2)
        checks["interrupted_cycle_reselected"] = recovery.get("action") == "abandoned_and_reselect" and not journal.path.exists()
        status = "passed" if all(checks.values()) else "failed"
    report = {
        "schema_version": 1,
        "kind": "runtime_reliability_check",
        "policy_version": RELIABILITY_POLICY_VERSION,
        "created_at": utc_now(),
        "status": status,
        "checks": checks,
        "api_requests": 0,
        "metered_model_requests": 0,
        "paid_spending": False,
    }
    artifact = root / "runtime" / "checks" / f"reliability_{uuid4().hex}.json"
    report["artifact"] = str(artifact)
    write_json(artifact, report)
    return report
