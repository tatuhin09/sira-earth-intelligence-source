"""Evaluator 1: protected final authorization gate for self-modification.

v1.0B-3a is deliberately non-promoting. The gate independently binds a
candidate to the current main public tree, re-checks protected-shell identity
and verification strength, and emits an auditable authorization checksum for a
future promotion step.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from .evaluator2 import EVALUATOR2_POLICY_VERSION
from .models import utc_now
from .self_modification import (
    PROTECTED_PATHS,
    PUBLIC_COPY_ENTRIES,
    SELF_MODIFICATION_POLICY_VERSION,
    _public_tree_digest,
)

EVALUATOR1_POLICY_VERSION = 1
SUPPORTED_EVALUATOR2_POLICIES = frozenset({1})
_IGNORED_NAMES = frozenset({"__pycache__", ".cache", ".pytest_cache"})


def _json_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _public_tree_has_symlink(root: Path) -> bool:
    root = Path(root).resolve()
    for entry in PUBLIC_COPY_ENTRIES:
        start = root / entry
        if start.is_symlink():
            return True
        if not start.exists() or start.is_file():
            continue
        for current, dirs, names in os.walk(start, followlinks=False):
            current_path = Path(current)
            kept_dirs: list[str] = []
            for name in dirs:
                child = current_path / name
                if name in _IGNORED_NAMES:
                    continue
                if child.is_symlink():
                    return True
                kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in names:
                if name in _IGNORED_NAMES:
                    continue
                if (current_path / name).is_symlink():
                    return True
    return False


def _protected_shell_digest(root: Path) -> str:
    root = Path(root).resolve()
    digest = hashlib.sha256()
    for relative in sorted(PROTECTED_PATHS):
        path = root.joinpath(*relative.split("/"))
        digest.update(relative.encode("utf-8") + b"\0")
        if path.is_symlink():
            digest.update(b"SYMLINK\0")
        elif path.is_file():
            digest.update(b"FILE\0" + path.read_bytes() + b"\0")
        elif path.exists():
            digest.update(b"OTHER\0")
        else:
            digest.update(b"MISSING\0")
    return digest.hexdigest()


def _load_candidate_manifest(candidate_root: Path) -> Mapping[str, object] | None:
    path = candidate_root.parent / "candidate_manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _health_summary(value: Mapping[str, object] | None) -> tuple[bool, int, int, int]:
    if not isinstance(value, Mapping) or value.get("overall_passed") is not True:
        return False, -1, -1, -1
    tests = value.get("tests")
    benchmark = value.get("benchmark")
    if not isinstance(tests, Mapping) or not isinstance(benchmark, Mapping):
        return False, -1, -1, -1
    if tests.get("passed") is not True or benchmark.get("passed") is not True:
        return False, -1, -1, -1
    test_count = tests.get("test_count")
    passed_cases = benchmark.get("passed_cases")
    failed_cases = benchmark.get("failed_cases")
    if any(type(item) is not int or item < 0 for item in (test_count, passed_cases, failed_cases)):
        return False, -1, -1, -1
    return True, test_count, passed_cases, failed_cases


def evaluate_final_gate(
    main_root: Path,
    candidate_root: Path,
    evaluator2_report: Mapping[str, object],
    baseline_health: Mapping[str, object],
    candidate_health: Mapping[str, object],
) -> dict[str, object]:
    """Issue Evaluator-1's non-promoting final authorization decision."""
    main_root = Path(main_root).resolve()
    raw_candidate = Path(candidate_root)
    if raw_candidate.is_symlink():
        raise ValueError("candidate root must not be a symlink")
    candidate_root = raw_candidate.resolve()
    if not main_root.is_dir() or not candidate_root.is_dir():
        raise ValueError("main and candidate roots must exist")

    e2_valid = bool(
        isinstance(evaluator2_report, Mapping)
        and evaluator2_report.get("kind") == "evaluator2_report"
        and evaluator2_report.get("policy_version") in SUPPORTED_EVALUATOR2_POLICIES
        and evaluator2_report.get("policy_version") == EVALUATOR2_POLICY_VERSION
        and evaluator2_report.get("decision") == "accept"
        and evaluator2_report.get("decision_code") == "candidate_verified"
        and evaluator2_report.get("promotion_recommended") is True
    )

    main_public_digest = _public_tree_digest(main_root)
    candidate_public_digest = _public_tree_digest(candidate_root)
    main_protected_digest = _protected_shell_digest(main_root)
    candidate_protected_digest = _protected_shell_digest(candidate_root)
    protected_unchanged = main_protected_digest == candidate_protected_digest
    symlink_free = not _public_tree_has_symlink(candidate_root)

    manifest = _load_candidate_manifest(candidate_root)
    manifest_valid = bool(
        manifest is not None
        and manifest.get("kind") == "candidate_workspace_manifest"
        and manifest.get("policy_version") == SELF_MODIFICATION_POLICY_VERSION
        and manifest.get("candidate_root") == str(candidate_root)
        and manifest.get("credentials_copied") is False
        and manifest.get("promotion_allowed") is False
        and isinstance(manifest.get("source_public_sha256"), str)
    )
    base_current = bool(manifest_valid and manifest.get("source_public_sha256") == main_public_digest)

    baseline_ok, baseline_tests, baseline_cases, baseline_failed = _health_summary(baseline_health)
    candidate_ok, candidate_tests, candidate_cases, candidate_failed = _health_summary(candidate_health)
    verification_not_weaker = bool(
        baseline_ok
        and candidate_ok
        and baseline_failed == 0
        and candidate_failed == 0
        and candidate_tests >= baseline_tests
        and candidate_cases >= baseline_cases
    )

    if not e2_valid:
        decision, code = "deny", "evaluator2_not_authorized"
    elif not manifest_valid or not symlink_free:
        decision, code = "deny", "candidate_integrity_failed"
    elif not base_current:
        decision, code = "deny", "stale_candidate_base"
    elif not protected_unchanged:
        decision, code = "deny", "protected_shell_mismatch"
    elif not verification_not_weaker:
        decision, code = "deny", "verification_regression"
    else:
        decision, code = "allow", "promotion_authorized"

    evaluator2_digest = _json_digest(evaluator2_report)
    fingerprint_material = {
        "evaluator1_policy_version": EVALUATOR1_POLICY_VERSION,
        "evaluator2_report_sha256": evaluator2_digest,
        "main_public_sha256": main_public_digest,
        "candidate_public_sha256": candidate_public_digest,
        "protected_shell_sha256": main_protected_digest,
    }
    fingerprint = _json_digest(fingerprint_material)

    return {
        "schema_version": 1,
        "kind": "evaluator1_gate_report",
        "policy_version": EVALUATOR1_POLICY_VERSION,
        "created_at": utc_now(),
        "decision": decision,
        "decision_code": code,
        "promotion_allowed": decision == "allow",
        "promotion_performed": False,
        "checks": {
            "evaluator2_authorized": e2_valid,
            "manifest_valid": manifest_valid,
            "candidate_base_current": base_current,
            "protected_shell_unchanged": protected_unchanged,
            "symlink_free": symlink_free,
            "baseline_healthy": baseline_ok,
            "candidate_healthy": candidate_ok,
            "verification_not_weaker": verification_not_weaker,
        },
        "verification": {
            "baseline_test_count": baseline_tests,
            "candidate_test_count": candidate_tests,
            "baseline_passed_cases": baseline_cases,
            "candidate_passed_cases": candidate_cases,
        },
        "authorization": {
            **fingerprint_material,
            "fingerprint": fingerprint,
            "checksum_only": True,
        },
    }
