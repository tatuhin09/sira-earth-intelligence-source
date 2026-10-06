"""Engineering failure circuit breaker (owner-enabled, stateless, advisory).

A trial of the autonomous loop made 11 code-change attempts in 55 minutes: all 11 were
rejected, every one asked a model to "reduce branch complexity", and each cost a model
request. When an opportunity type fails repeatedly with no success in between, more
attempts of that type only burn quota. This breaker reads the persisted writer handoffs,
counts the unbroken trailing run of failures per opportunity type and, once the owner-set
threshold is reached, hides that type from discovery for a growing hold period. A single
promoted result resets the run. Infrastructure outcomes are neither failures nor successes.

It only filters opportunities. It cannot approve, promote or modify anything, and the
decision is derived from artifacts, so a restart or a deleted file never leaves stale
state. Disabled unless ``memory/engineering_breaker/policy.json`` enables it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

POLICY_SCHEMA = "sira.engineering_breaker_policy.v1"
FAILURE_OUTCOMES = frozenset({"candidate_rejected", "structural_goal_not_met"})
MAX_HANDOFF_FILES = 400
MAX_FILE_BYTES = 256 * 1024
BASE_HOLD_SECONDS = 3600
LOOKBACK_SECONDS = 7 * 86_400


def _directory(root: Path) -> Path:
    return Path(root) / "memory" / "engineering_breaker"


def default_policy() -> dict:
    return {"enabled": False, "failure_threshold": 5, "max_hold_hours": 24,
            "valid": True, "source": "default"}


def _policy_ok(value: Mapping) -> bool:
    return (type(value["enabled"]) is bool
            and type(value["failure_threshold"]) is int and 2 <= value["failure_threshold"] <= 30
            and type(value["max_hold_hours"]) is int and 1 <= value["max_hold_hours"] <= 168)


def load_policy(root: Path) -> dict:
    path = _directory(root) / "policy.json"
    if not path.exists() and not path.is_symlink():
        return default_policy()
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2048:
            raise ValueError("unsafe policy")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema") != POLICY_SCHEMA
                or set(data) != {"schema", "enabled", "failure_threshold", "max_hold_hours"}
                or not _policy_ok(data)):
            raise ValueError("invalid policy")
    except (OSError, ValueError, UnicodeError, TypeError, KeyError):
        return {**default_policy(), "valid": False, "source": "malformed"}
    return {"enabled": data["enabled"], "failure_threshold": data["failure_threshold"],
            "max_hold_hours": data["max_hold_hours"], "valid": True, "source": "owner_policy"}


def write_policy(root: Path, *, enabled: bool, failure_threshold: int = 5,
                 max_hold_hours: int = 24) -> dict:
    candidate = {"enabled": enabled, "failure_threshold": failure_threshold,
                 "max_hold_hours": max_hold_hours}
    if not _policy_ok(candidate):
        raise ValueError("failure_threshold must be 2..30 and max_hold_hours 1..168")
    directory = _directory(root)
    if directory.is_symlink() or Path(root, "memory").is_symlink():
        raise ValueError("unsafe policy directory")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".policy.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps({"schema": POLICY_SCHEMA, **candidate}), encoding="utf-8")
    os.replace(temporary, directory / "policy.json")
    return load_policy(root)


def _attempts(root: Path, now_epoch: float) -> dict[str, list[tuple[float, str]]]:
    """Per opportunity type: (attempted_at_epoch, "failure"|"success") oldest first."""
    directory = Path(root) / "improvements" / "opportunities" / "handoffs"
    found: dict[str, list[tuple[float, str]]] = {}
    if not directory.is_dir() or directory.is_symlink():
        return found
    paths = sorted(directory.glob("ohf_*.json"), key=lambda p: p.stat().st_mtime,
                   reverse=True)[:MAX_HANDOFF_FILES]
    for path in paths:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        cooldown = data.get("cooldown") if isinstance(data, dict) else None
        if not isinstance(cooldown, dict):
            continue
        kind, when = cooldown.get("type"), cooldown.get("attempted_at_epoch")
        if (not isinstance(kind, str) or isinstance(when, bool)
                or not isinstance(when, (int, float))
                or not 0 <= now_epoch - when <= LOOKBACK_SECONDS):
            continue
        outcome = str(data.get("outcome") or cooldown.get("outcome") or "")
        if data.get("promotion_performed") is True or outcome == "promotion_committed":
            label = "success"
        elif outcome in FAILURE_OUTCOMES:
            label = "failure"
        else:
            continue  # infrastructure or unknown outcomes neither count nor reset
        found.setdefault(kind, []).append((float(when), label))
    for rows in found.values():
        rows.sort()
    return found


def breaker_state(root: Path, *, now_epoch: float) -> dict[str, dict]:
    """Open breakers by opportunity type: streak, hold seconds and the time they close."""
    policy = load_policy(root)
    if not (policy["valid"] and policy["enabled"]):
        return {}
    threshold = policy["failure_threshold"]
    cap = policy["max_hold_hours"] * 3600
    state: dict[str, dict] = {}
    for kind, rows in _attempts(Path(root), now_epoch).items():
        streak = 0
        for _when, label in reversed(rows):
            if label != "failure":
                break
            streak += 1
        if streak < threshold:
            continue
        hold = min(cap, BASE_HOLD_SECONDS * 2 ** (streak - threshold))
        reopens = rows[-1][0] + hold
        if now_epoch < reopens:
            state[kind] = {"streak": streak, "hold_seconds": hold, "reopens_at_epoch": reopens}
    return state


def filter_opportunities(root: Path, report: Mapping[str, Any], *, now_epoch: float) -> dict:
    """Return the discovery report without opportunities of open-breaker types."""
    state = breaker_state(root, now_epoch=now_epoch)
    result = dict(report)
    result["failure_breaker"] = {"policy": load_policy(root)["source"], "open_types": state,
                                 "suppressed_count": 0}
    if not state or not isinstance(report.get("opportunities"), list):
        return result
    kept = [row for row in report["opportunities"]
            if not (isinstance(row, Mapping) and row.get("type") in state)]
    result["opportunities"] = kept
    result["failure_breaker"]["suppressed_count"] = len(report["opportunities"]) - len(kept)
    return result
