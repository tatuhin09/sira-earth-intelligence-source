"""Evaluator 2: deterministic candidate diff and regression gate.

This evaluator is intentionally non-promoting. It inspects only the public
candidate tree, combines structural diff checks with already-measured tests and
benchmarks, and returns a bounded accept/reject/block decision for the next gate.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Mapping

from .auto_evaluator import compare_health
from .models import utc_now
from .self_modification import (
    MAX_EDIT_FILE_BYTES,
    MAX_EDIT_FILES,
    MAX_EDIT_TOTAL_BYTES,
    MODIFIABLE_ROOTS,
    PROTECTED_PATHS,
    PUBLIC_COPY_ENTRIES,
)

EVALUATOR2_POLICY_VERSION = 1
_IGNORED_NAMES = frozenset({"__pycache__", ".cache", ".pytest_cache"})
_IGNORED_SUFFIXES = (".pyc", ".pyo")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _scan_public_tree(root: Path) -> tuple[dict[str, dict[str, object]], set[str]]:
    """Return public-file metadata plus symlink paths without following links."""
    root = Path(root).resolve()
    files: dict[str, dict[str, object]] = {}
    symlinks: set[str] = set()

    for entry in PUBLIC_COPY_ENTRIES:
        start = root / entry
        if not start.exists() and not start.is_symlink():
            continue
        if start.is_symlink():
            symlinks.add(entry)
            continue
        if start.is_file():
            payload = start.read_bytes()
            files[entry] = {"size": len(payload), "sha256": _sha256(payload), "utf8": _is_utf8(payload)}
            continue

        for current, dirs, names in os.walk(start, followlinks=False):
            current_path = Path(current)
            kept_dirs: list[str] = []
            for name in dirs:
                child = current_path / name
                rel = child.relative_to(root).as_posix()
                if name in _IGNORED_NAMES:
                    continue
                if child.is_symlink():
                    symlinks.add(rel)
                    continue
                kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in names:
                if name in _IGNORED_NAMES or name.endswith(_IGNORED_SUFFIXES):
                    continue
                child = current_path / name
                rel = child.relative_to(root).as_posix()
                if child.is_symlink():
                    symlinks.add(rel)
                    continue
                if not child.is_file():
                    continue
                payload = child.read_bytes()
                files[rel] = {"size": len(payload), "sha256": _sha256(payload), "utf8": _is_utf8(payload)}
    return files, symlinks


def _is_utf8(payload: bytes) -> bool:
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return b"\x00" not in payload


def _is_modifiable(path: str) -> bool:
    return any(path.startswith(root + "/") for root in MODIFIABLE_ROOTS)


def _health_passed(value: Mapping[str, object] | None) -> bool:
    return bool(isinstance(value, Mapping) and value.get("overall_passed") is True)


def evaluate_candidate(
    main_root: Path,
    candidate_root: Path,
    baseline_health: Mapping[str, object],
    candidate_health: Mapping[str, object],
) -> dict[str, object]:
    """Inspect one candidate and issue Evaluator-2's non-promoting decision."""
    main_root = Path(main_root).resolve()
    raw_candidate = Path(candidate_root)
    if raw_candidate.is_symlink():
        raise ValueError("candidate root must not be a symlink")
    candidate_root = raw_candidate.resolve()
    if not main_root.is_dir() or not candidate_root.is_dir():
        raise ValueError("main and candidate roots must exist")

    main_files, main_symlinks = _scan_public_tree(main_root)
    candidate_files, candidate_symlinks = _scan_public_tree(candidate_root)

    main_paths = set(main_files)
    candidate_paths = set(candidate_files)
    added = sorted(candidate_paths - main_paths)
    removed = sorted(main_paths - candidate_paths)
    modified = sorted(
        path for path in (main_paths & candidate_paths)
        if main_files[path]["sha256"] != candidate_files[path]["sha256"]
    )
    changed = sorted(set(added) | set(removed) | set(modified) | candidate_symlinks | main_symlinks)

    risk_flags: set[str] = set()
    if candidate_symlinks:
        risk_flags.add("symlink_present")
    if removed:
        risk_flags.add("file_removed")
    if any(path in PROTECTED_PATHS for path in changed):
        risk_flags.add("protected_path_changed")
    if any(not _is_modifiable(path) for path in changed):
        risk_flags.add("unapproved_path_changed")

    changed_regular = [path for path in sorted(set(added) | set(modified)) if path in candidate_files]
    if any(not bool(candidate_files[path]["utf8"]) for path in changed_regular):
        risk_flags.add("non_utf8_text")
    changed_bytes = sum(int(candidate_files[path]["size"]) for path in changed_regular)
    if len(changed) > MAX_EDIT_FILES:
        risk_flags.add("too_many_changed_files")
    if any(int(candidate_files[path]["size"]) > MAX_EDIT_FILE_BYTES for path in changed_regular):
        risk_flags.add("oversized_changed_file")
    if changed_bytes > MAX_EDIT_TOTAL_BYTES:
        risk_flags.add("oversized_change_batch")

    baseline_ok = _health_passed(baseline_health)
    candidate_ok = _health_passed(candidate_health)
    verification = compare_health(baseline_health, candidate_health)
    protected_unchanged = not any(path in PROTECTED_PATHS for path in changed)
    paths_allowed = all(_is_modifiable(path) for path in changed) if changed else True
    text_only = all(bool(candidate_files[path]["utf8"]) for path in changed_regular)
    bounded_diff = not any(flag in risk_flags for flag in {
        "too_many_changed_files", "oversized_changed_file", "oversized_change_batch"
    })
    structural_ok = not risk_flags

    decisions = (
        (not baseline_ok, ("block", "unhealthy_baseline")),
        (not candidate_ok, ("reject", "candidate_regression")),
        (verification.get("classification") == "verification_shrinkage", ("reject", "verification_shrinkage")),
        (not structural_ok, ("reject", "structural_risk")),
        (not changed, ("accept", "no_change_verified")),
    )
    decision, code = next((d for cond, d in decisions if cond), ("accept", "candidate_verified"))

    return {
        "schema_version": 1,
        "kind": "evaluator2_report",
        "policy_version": EVALUATOR2_POLICY_VERSION,
        "created_at": utc_now(),
        "decision": decision,
        "decision_code": code,
        "promotion_recommended": decision == "accept" and code == "candidate_verified",
        "evaluation_classification": verification.get("classification"),
        "verification": verification,
        "risk_flags": sorted(risk_flags),
        "checks": {
            "baseline_healthy": baseline_ok,
            "candidate_healthy": candidate_ok,
            "paths_allowed": paths_allowed,
            "protected_unchanged": protected_unchanged,
            "text_only": text_only,
            "symlink_free": not candidate_symlinks,
            "bounded_diff": bounded_diff,
        },
        "diff": {
            "added_files": added,
            "modified_files": modified,
            "removed_files": removed,
            "symlink_paths": sorted(candidate_symlinks),
            "changed_files": changed,
            "changed_file_count": len(changed),
            "changed_bytes": changed_bytes,
        },
    }
