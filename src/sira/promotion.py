"""Protected transactional promotion gate with automatic rollback.

A promotion is permitted only when a fresh Evaluator-1 authorization still
binds the exact current main tree, exact candidate tree, protected shell and
Evaluator-2 report. The operation snapshots every changed file first, applies
bounded replacements, then runs a trusted post-promotion verifier. Any failed
or exceptional verification restores the exact pre-promotion files.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import shutil
from typing import Callable, Mapping
from uuid import uuid4

from .evaluator1 import (
    EVALUATOR1_POLICY_VERSION,
    _json_digest,
    _protected_shell_digest,
)
from .models import utc_now
from .rollback import BackupEntry, RollbackError, create_backup, restore_backup
from .self_modification import MODIFIABLE_ROOTS, PROTECTED_PATHS, _public_tree_digest
from .storage import write_json

PROMOTION_POLICY_VERSION = 1

PostPromotionVerifier = Callable[[Path], Mapping[str, object]]


def _allowed_changed_path(relative: str) -> bool:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        return False
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return False
    normalized = path.as_posix()
    return (
        normalized not in PROTECTED_PATHS
        and any(normalized.startswith(root + "/") for root in MODIFIABLE_ROOTS)
    )


def _safe_candidate_file(candidate_root: Path, relative: str) -> Path:
    candidate_root = Path(candidate_root).resolve()
    path = PurePosixPath(relative)
    current = candidate_root
    for part in path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("candidate promotion path traverses a symlink")
    source = candidate_root.joinpath(*path.parts)
    if source.is_symlink() or not source.is_file():
        raise ValueError("candidate promotion source must be a regular file")
    try:
        source.parent.resolve().relative_to(candidate_root)
    except ValueError:
        raise ValueError("candidate promotion source escapes candidate root") from None
    return source


def _safe_main_target(main_root: Path, relative: str) -> Path:
    main_root = Path(main_root).resolve()
    path = PurePosixPath(relative)
    current = main_root
    for part in path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("main promotion path traverses a symlink")
    target = main_root.joinpath(*path.parts)
    if target.is_symlink():
        raise ValueError("main promotion target is a symlink")
    try:
        target.parent.resolve().relative_to(main_root)
    except ValueError:
        raise ValueError("main promotion target escapes project root") from None
    return target


def _authorization_state(
    main_root: Path,
    candidate_root: Path,
    evaluator1_report: Mapping[str, object],
    evaluator2_report: Mapping[str, object],
) -> tuple[bool, str, list[str], dict[str, str]]:
    if not isinstance(evaluator1_report, Mapping) or not isinstance(evaluator2_report, Mapping):
        return False, "authorization_mismatch", [], {}
    authorization = evaluator1_report.get("authorization")
    if not isinstance(authorization, Mapping):
        return False, "authorization_mismatch", [], {}
    e1_valid = bool(
        evaluator1_report.get("kind") == "evaluator1_gate_report"
        and evaluator1_report.get("policy_version") == EVALUATOR1_POLICY_VERSION
        and evaluator1_report.get("decision") == "allow"
        and evaluator1_report.get("decision_code") == "promotion_authorized"
        and evaluator1_report.get("promotion_allowed") is True
        and evaluator1_report.get("promotion_performed") is False
        and authorization.get("checksum_only") is True
    )
    if not e1_valid:
        return False, "authorization_mismatch", [], {}

    diff = evaluator2_report.get("diff")
    changed_files = diff.get("changed_files") if isinstance(diff, Mapping) else None
    if (
        evaluator2_report.get("kind") != "evaluator2_report"
        or evaluator2_report.get("decision") != "accept"
        or evaluator2_report.get("decision_code") != "candidate_verified"
        or evaluator2_report.get("promotion_recommended") is not True
        or not isinstance(changed_files, list)
        or not changed_files
        or any(not _allowed_changed_path(path) for path in changed_files)
    ):
        return False, "authorization_mismatch", [], {}

    evaluator2_digest = _json_digest(evaluator2_report)
    main_public = _public_tree_digest(main_root)
    candidate_public = _public_tree_digest(candidate_root)
    protected = _protected_shell_digest(main_root)
    material = {
        "evaluator1_policy_version": EVALUATOR1_POLICY_VERSION,
        "evaluator2_report_sha256": evaluator2_digest,
        "main_public_sha256": main_public,
        "candidate_public_sha256": candidate_public,
        "protected_shell_sha256": protected,
    }
    fingerprint = _json_digest(material)

    if authorization.get("evaluator2_report_sha256") != evaluator2_digest:
        return False, "authorization_mismatch", [], material
    if authorization.get("candidate_public_sha256") != candidate_public:
        return False, "stale_authorization", [], material
    if authorization.get("protected_shell_sha256") != protected:
        return False, "stale_authorization", [], material
    if authorization.get("main_public_sha256") != main_public:
        return False, "stale_authorization", [], material
    if authorization.get("fingerprint") != fingerprint:
        return False, "authorization_mismatch", [], material

    return True, "authorized", sorted(set(changed_files)), material


def _post_verification_passed(value: object) -> bool:
    return bool(isinstance(value, Mapping) and value.get("overall_passed") is True)


def _persist_report(main_root: Path, promotion_id: str, report: dict[str, object]) -> str:
    path = Path(main_root).resolve() / "runtime" / "promotions" / promotion_id / "promotion_report.json"
    report["audit_path"] = str(path)
    write_json(path, report)
    return str(path)


def _rollback(
    main_root: Path,
    backup_root: Path,
    entries: list[BackupEntry],
    expected_pre_digest: str,
) -> tuple[bool, str | None]:
    try:
        restore_backup(main_root, backup_root, entries)
    except RollbackError as exc:
        return False, type(exc).__name__
    return _public_tree_digest(main_root) == expected_pre_digest, None


def promote_candidate(
    main_root: Path,
    candidate_root: Path,
    evaluator1_report: Mapping[str, object],
    evaluator2_report: Mapping[str, object],
    post_promotion_verifier: PostPromotionVerifier,
) -> dict[str, object]:
    """Transactionally promote one authorized candidate or restore the old tree."""
    main_root = Path(main_root).resolve()
    raw_candidate = Path(candidate_root)
    if raw_candidate.is_symlink():
        raise ValueError("candidate root must not be a symlink")
    candidate_root = raw_candidate.resolve()
    if not main_root.is_dir() or not candidate_root.is_dir():
        raise ValueError("main and candidate roots must exist")
    if not callable(post_promotion_verifier):
        raise TypeError("post_promotion_verifier must be callable")

    promotion_id = "pr_" + uuid4().hex
    created_at = utc_now()
    authorized, authorization_code, changed_files, material = _authorization_state(
        main_root,
        candidate_root,
        evaluator1_report,
        evaluator2_report,
    )
    pre_public = _public_tree_digest(main_root)
    protected_before = _protected_shell_digest(main_root)

    if not authorized:
        report: dict[str, object] = {
            "schema_version": 1,
            "kind": "promotion_report",
            "policy_version": PROMOTION_POLICY_VERSION,
            "promotion_id": promotion_id,
            "created_at": created_at,
            "status": "denied",
            "decision_code": authorization_code,
            "promotion_performed": False,
            "rollback_performed": False,
            "rollback_verified": False,
            "changed_files": [],
            "pre_public_sha256": pre_public,
            "target_public_sha256": material.get("candidate_public_sha256"),
            "final_public_sha256": pre_public,
            "protected_shell_sha256": protected_before,
            "post_verification": None,
        }
        _persist_report(main_root, promotion_id, report)
        return report

    # Preflight all source and destination paths before creating the backup.
    sources: list[tuple[str, Path, Path]] = []
    for relative in changed_files:
        source = _safe_candidate_file(candidate_root, relative)
        target = _safe_main_target(main_root, relative)
        # Evaluator-2 already checks UTF-8; re-read here to close the TOCTOU gap.
        payload = source.read_bytes()
        if b"\x00" in payload:
            raise ValueError("candidate promotion source is not text")
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("candidate promotion source is not UTF-8") from None
        sources.append((relative, source, target))

    audit_root = main_root / "runtime" / "promotions" / promotion_id
    backup_root = audit_root / "backup"
    entries = create_backup(main_root, backup_root, changed_files)

    write_error: str | None = None
    try:
        for relative, source, target in sources:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".sira-promote.tmp")
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
            shutil.copy2(source, temporary, follow_symlinks=False)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, target)
    except Exception as exc:  # Fail closed and restore from protected backup.
        write_error = type(exc).__name__

    target_public = material["candidate_public_sha256"]
    structure_ok = bool(
        write_error is None
        and _public_tree_digest(main_root) == target_public
        and _protected_shell_digest(main_root) == protected_before
    )

    verification: Mapping[str, object] | None = None
    verification_error: str | None = None
    if structure_ok:
        try:
            result = post_promotion_verifier(main_root)
            verification = result if isinstance(result, Mapping) else None
        except Exception as exc:  # The verifier is trusted, but failure still rolls back.
            verification_error = type(exc).__name__

    verified = bool(
        structure_ok
        and verification_error is None
        and _post_verification_passed(verification)
        and _public_tree_digest(main_root) == target_public
        and _protected_shell_digest(main_root) == protected_before
    )

    if verified:
        report = {
            "schema_version": 1,
            "kind": "promotion_report",
            "policy_version": PROMOTION_POLICY_VERSION,
            "promotion_id": promotion_id,
            "created_at": created_at,
            "status": "promoted",
            "decision_code": "promotion_committed",
            "promotion_performed": True,
            "rollback_performed": False,
            "rollback_verified": False,
            "changed_files": changed_files,
            "pre_public_sha256": pre_public,
            "target_public_sha256": target_public,
            "final_public_sha256": _public_tree_digest(main_root),
            "protected_shell_sha256": protected_before,
            "post_verification": dict(verification) if verification is not None else None,
        }
        _persist_report(main_root, promotion_id, report)
        return report

    rollback_verified, rollback_error = _rollback(main_root, backup_root, entries, pre_public)
    if write_error is not None or not structure_ok:
        code = "promotion_write_failed"
    elif verification_error is not None:
        code = "post_verification_error"
    else:
        code = "post_verification_failed"
    status = "rolled_back" if rollback_verified else "rollback_failed"
    report = {
        "schema_version": 1,
        "kind": "promotion_report",
        "policy_version": PROMOTION_POLICY_VERSION,
        "promotion_id": promotion_id,
        "created_at": created_at,
        "status": status,
        "decision_code": code if rollback_verified else "rollback_incomplete",
        "promotion_performed": False,
        "rollback_performed": True,
        "rollback_verified": rollback_verified,
        "changed_files": changed_files,
        "pre_public_sha256": pre_public,
        "target_public_sha256": target_public,
        "final_public_sha256": _public_tree_digest(main_root),
        "protected_shell_sha256": _protected_shell_digest(main_root),
        "post_verification": dict(verification) if verification is not None else None,
        "verification_error": verification_error,
        "write_error": write_error,
        "rollback_error": rollback_error,
        "requires_manual_recovery": not rollback_verified,
    }
    _persist_report(main_root, promotion_id, report)
    return report
