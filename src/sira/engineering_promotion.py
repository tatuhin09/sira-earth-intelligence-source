"""Protected transactional promotion for authorized engineering candidates.

v1.8H is deliberately separate from SIRA's existing Python self-promotion
pipeline. It consumes a fresh v1.8G checksum authorization, snapshots exactly
the declared engineering source files, atomically replaces those files, binds
the resulting main engineering surface to the authorized candidate checksum,
then verifies a fresh isolated copy of the promoted main surface.

Any write, structural, copy, verifier, or verification failure restores the
exact pre-promotion source files. This module never executes build/test commands
inside the main project tree and never installs dependencies.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
from typing import Any, Callable, Mapping
from uuid import uuid4

from .engineering_authorization import (
    ENGINEERING_AUTHORIZATION_POLICY_VERSION,
    _IGNORED_DIRS,
    _copyable_relative,
    _safe_declared_path,
    _surface_snapshot,
    evaluate_engineering_authorization,
)
from .engineering_verification import execute_verification_plan
from .models import utc_now
from .rollback import BackupEntry, RollbackError, create_backup, restore_backup
from .storage import write_json

ENGINEERING_PROMOTION_POLICY_VERSION = 1
MAX_PROMOTION_FILES = 12
MAX_PROMOTION_FILE_BYTES = 256 * 1024
MAX_PROMOTION_TOTAL_BYTES = 768 * 1024

CommandRunner = Callable[..., Mapping[str, Any]]


class EngineeringPromotionError(RuntimeError):
    """Raised when protected promotion preconditions are malformed."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_roots(
    main_root: Path,
    candidate_root: Path,
) -> tuple[Path, Path]:
    main = Path(main_root).resolve()
    raw_candidate = Path(candidate_root)
    if raw_candidate.is_symlink():
        raise EngineeringPromotionError(
            "engineering candidate root must not be a symlink"
        )
    candidate = raw_candidate.resolve()
    if not main.is_dir() or not candidate.is_dir():
        raise EngineeringPromotionError(
            "engineering main and candidate roots must exist"
        )
    if candidate == main:
        raise EngineeringPromotionError(
            "engineering candidate cannot equal main project"
        )
    try:
        candidate.relative_to(main)
    except ValueError:
        return main, candidate
    raise EngineeringPromotionError(
        "engineering candidate must be outside main project"
    )


def _prepare_transaction_root(
    transaction_root: Path,
    main_root: Path,
    candidate_root: Path,
) -> Path:
    raw = Path(transaction_root)
    if raw.exists() or raw.is_symlink():
        raise EngineeringPromotionError(
            "engineering transaction root must not already exist"
        )
    parent = raw.parent.resolve()
    if not parent.is_dir():
        raise EngineeringPromotionError(
            "engineering transaction parent must exist"
        )
    transaction = raw.absolute()

    resolved_future = parent / raw.name
    for forbidden, label in (
        (main_root, "main project"),
        (candidate_root, "candidate"),
    ):
        try:
            resolved_future.relative_to(forbidden)
        except ValueError:
            pass
        else:
            raise EngineeringPromotionError(
                f"engineering transaction root cannot be inside {label}"
            )

    transaction.mkdir(mode=0o700, exist_ok=False)
    return transaction.resolve()


def _safe_candidate_file(
    candidate_root: Path,
    relative: str,
    expected_sha256: str,
) -> tuple[Path, bytes]:
    if not _safe_declared_path(relative):
        raise EngineeringPromotionError(
            "authorization contains unsafe engineering path"
        )
    path = PurePosixPath(relative)
    current = candidate_root
    for part in path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise EngineeringPromotionError(
                "engineering candidate path traverses a symlink"
            )
    source = candidate_root.joinpath(*path.parts)
    if source.is_symlink() or not source.is_file():
        raise EngineeringPromotionError(
            "engineering candidate source must be a regular file"
        )
    try:
        source.parent.resolve().relative_to(candidate_root)
    except ValueError:
        raise EngineeringPromotionError(
            "engineering candidate source escapes candidate root"
        ) from None

    payload = source.read_bytes()
    if (
        len(payload) > MAX_PROMOTION_FILE_BYTES
        or b"\x00" in payload
    ):
        raise EngineeringPromotionError(
            "engineering promotion source violates text/size policy"
        )
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        raise EngineeringPromotionError(
            "engineering promotion source must be UTF-8"
        ) from None
    if _sha256(payload) != expected_sha256:
        raise EngineeringPromotionError(
            "engineering promotion source hash mismatch"
        )
    return source, payload


def _safe_main_target(
    main_root: Path,
    relative: str,
) -> Path:
    if not _safe_declared_path(relative):
        raise EngineeringPromotionError(
            "engineering promotion target is not allowed"
        )
    path = PurePosixPath(relative)
    current = main_root
    for part in path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise EngineeringPromotionError(
                "engineering main path traverses a symlink"
            )
    target = main_root.joinpath(*path.parts)
    if target.is_symlink():
        raise EngineeringPromotionError(
            "engineering main target is a symlink"
        )
    try:
        target.parent.resolve().relative_to(main_root)
    except ValueError:
        raise EngineeringPromotionError(
            "engineering main target escapes project root"
        ) from None
    if target.exists() and not target.is_file():
        raise EngineeringPromotionError(
            "engineering main target must be a regular file or absent"
        )
    return target


def _authorization_shape_valid(
    report: Mapping[str, object],
) -> bool:
    authorization = report.get("authorization")
    checks = report.get("checks")
    failures = report.get("failures")
    return bool(
        report.get("schema")
        == "sira.engineering_authorization_gate.v1"
        and report.get("policy_version")
        == ENGINEERING_AUTHORIZATION_POLICY_VERSION
        and report.get("decision") == "allow"
        and report.get("decision_code")
        == "engineering_promotion_authorized"
        and report.get("promotion_allowed") is True
        and report.get("promotion_performed") is False
        and report.get("authority_scope")
        == "engineering_candidate_checksum_only"
        and isinstance(checks, Mapping)
        and bool(checks)
        and all(value is True for value in checks.values())
        and isinstance(failures, list)
        and not failures
        and isinstance(authorization, Mapping)
        and authorization.get("checksum_only") is True
        and authorization.get("write_authority") is False
        and authorization.get(
            "promotion_execution_authority"
        )
        is False
        and isinstance(authorization.get("fingerprint"), str)
        and len(str(authorization.get("fingerprint"))) == 64
    )


def _fresh_authorization_state(
    main_root: Path,
    writer_attempt: Mapping[str, object],
    evaluator_report: Mapping[str, object],
    supplied_authorization: Mapping[str, object],
) -> tuple[
    bool,
    str,
    Mapping[str, object] | None,
]:
    if not _authorization_shape_valid(supplied_authorization):
        return False, "authorization_mismatch", None

    fresh = evaluate_engineering_authorization(
        main_root,
        writer_attempt,
        evaluator_report,
    )
    if fresh.get("decision") != "allow":
        code = fresh.get("decision_code")
        return (
            False,
            str(code)
            if isinstance(code, str) and code
            else "stale_authorization",
            fresh,
        )
    if not _authorization_shape_valid(fresh):
        return False, "authorization_mismatch", fresh

    supplied_auth = supplied_authorization.get("authorization")
    fresh_auth = fresh.get("authorization")
    supplied_binding = supplied_authorization.get("binding")
    fresh_binding = fresh.get("binding")
    if not (
        isinstance(supplied_auth, Mapping)
        and isinstance(fresh_auth, Mapping)
        and isinstance(supplied_binding, Mapping)
        and isinstance(fresh_binding, Mapping)
    ):
        return False, "authorization_mismatch", fresh

    immutable_keys = {
        "fingerprint",
        "engineering_authorization_policy_version",
        "engineering_evaluator_report_sha256",
        "engineering_writer_attempt_sha256",
        "main_source_sha256",
        "candidate_source_sha256",
        "changed_files_sha256",
        "verification_sha256",
        "checksum_only",
        "write_authority",
        "promotion_execution_authority",
    }
    if any(
        supplied_auth.get(key) != fresh_auth.get(key)
        for key in immutable_keys
    ):
        return False, "authorization_mismatch", fresh

    binding_keys = {
        "source_manifest_sha256",
        "main_source_sha256",
        "candidate_source_sha256",
        "changed_files",
        "changed_file_sha256",
        "changed_bytes",
    }
    if any(
        supplied_binding.get(key) != fresh_binding.get(key)
        for key in binding_keys
    ):
        return False, "authorization_mismatch", fresh

    return True, "authorized", fresh


def _atomic_replace(
    target: Path,
    payload: bytes,
) -> None:
    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    temporary = target.with_name(
        target.name
        + ".sira-eng-promote.tmp"
    )
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()

    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            directory_fd = os.open(
                target.parent,
                os.O_RDONLY,
            )
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if temporary.exists() or temporary.is_symlink():
            try:
                temporary.unlink()
            except OSError:
                pass


def _copy_promoted_surface(
    main_root: Path,
    destination: Path,
) -> None:
    if destination.exists() or destination.is_symlink():
        raise EngineeringPromotionError(
            "post-verification candidate must not already exist"
        )
    destination.mkdir(mode=0o700, exist_ok=False)

    for current, dirs, names in os.walk(
        main_root,
        followlinks=False,
    ):
        current_path = Path(current)
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in _IGNORED_DIRS
            and not name.startswith(".")
            and not (current_path / name).is_symlink()
        )
        for name in sorted(names):
            source = current_path / name
            if source.is_symlink() or not source.is_file():
                continue
            relative = PurePosixPath(
                source.relative_to(main_root).as_posix()
            )
            if not _copyable_relative(relative):
                continue
            payload = source.read_bytes()
            # _surface_snapshot ignores files above its per-file copy bound.
            # The post-verify copy must reproduce that exact surface.
            if len(payload) > 1024 * 1024:
                continue
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            shutil.copy2(
                source,
                target,
                follow_symlinks=False,
            )


def _verification_passed(
    result: object,
) -> bool:
    if not isinstance(result, Mapping):
        return False
    commands = result.get("commands")
    return bool(
        result.get("schema")
        == "sira.engineering_verification_result.v1"
        and result.get("status") == "completed"
        and result.get("outcome")
        == "verification_passed"
        and result.get("overall_passed") is True
        and result.get("diagnostic_count") == 0
        and result.get("missing_command_ids") == []
        and result.get("processes_executed", 0) >= 1
        and result.get("shell_used") is False
        and result.get("network_isolation_enforced") is True
        and result.get("credentials_inherited") is False
        and result.get(
            "package_installation_performed"
        )
        is False
        and result.get("main_tree_modified") is False
        and result.get("authority_granted") is False
        and result.get("promotion_authorized") is False
        and result.get("raw_output_included") is False
        and isinstance(commands, list)
        and bool(commands)
        and all(
            isinstance(row, Mapping)
            and row.get("status") == "passed"
            and row.get("returncode") == 0
            and row.get("timed_out") is False
            and row.get("output_limit_exceeded") is False
            for row in commands
        )
    )


def _verification_summary(
    value: object,
) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    commands = value.get("commands")
    command_rows: list[dict[str, object]] = []
    if isinstance(commands, list):
        for row in commands:
            if not isinstance(row, Mapping):
                continue
            command_rows.append({
                "command_id": row.get("command_id"),
                "kind": row.get("kind"),
                "language": row.get("language"),
                "status": row.get("status"),
                "returncode": row.get("returncode"),
                "timed_out": row.get("timed_out"),
                "output_limit_exceeded":
                    row.get("output_limit_exceeded"),
                "output_bytes": row.get("output_bytes"),
                "output_sha256": row.get("output_sha256"),
                "diagnostic_count":
                    row.get("diagnostic_count"),
            })
    return {
        "schema": value.get("schema"),
        "status": value.get("status"),
        "outcome": value.get("outcome"),
        "overall_passed": value.get("overall_passed"),
        "isolation_backend": value.get("isolation_backend"),
        "processes_executed": value.get("processes_executed"),
        "diagnostic_count": value.get("diagnostic_count"),
        "missing_command_ids": value.get("missing_command_ids"),
        "commands": command_rows,
        "raw_output_included": False,
    }


def _restore_and_verify(
    main_root: Path,
    backup_root: Path,
    entries: list[BackupEntry],
    expected_pre_source_sha: str,
) -> tuple[bool, str | None]:
    try:
        restore_backup(
            main_root,
            backup_root,
            entries,
        )
    except RollbackError as exc:
        return False, type(exc).__name__
    try:
        restored_sha, _ = _surface_snapshot(
            main_root
        )
    except ValueError as exc:
        return False, type(exc).__name__
    return (
        restored_sha
        == expected_pre_source_sha,
        None,
    )


def _persist_report(
    transaction_root: Path,
    report: dict[str, object],
) -> str:
    path = (
        transaction_root
        / "engineering_promotion_report.json"
    )
    report["audit_path"] = str(path)
    write_json(path, report)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return str(path)


def _cleanup_transaction_artifacts(
    transaction_root: Path,
    *,
    keep_backup: bool,
) -> None:
    postverify = (
        transaction_root
        / "postverify_candidate"
    )
    if postverify.exists() and not postverify.is_symlink():
        shutil.rmtree(postverify)
    backup = transaction_root / "backup"
    if (
        not keep_backup
        and backup.exists()
        and not backup.is_symlink()
    ):
        shutil.rmtree(backup)


def promote_engineering_candidate(
    main_root: Path,
    writer_attempt: Mapping[str, object],
    evaluator_report: Mapping[str, object],
    authorization_report: Mapping[str, object],
    transaction_root: Path,
    *,
    command_runner: CommandRunner | None = None,
    bubblewrap_path: str | None = None,
) -> dict[str, object]:
    """Transactionally promote one freshly authorized engineering candidate."""
    if not isinstance(writer_attempt, Mapping):
        raise EngineeringPromotionError(
            "writer attempt must be a mapping"
        )
    if not isinstance(evaluator_report, Mapping):
        raise EngineeringPromotionError(
            "evaluator report must be a mapping"
        )
    if not isinstance(authorization_report, Mapping):
        raise EngineeringPromotionError(
            "authorization report must be a mapping"
        )

    candidate_value = writer_attempt.get(
        "candidate_root"
    )
    if not isinstance(candidate_value, str) or not candidate_value:
        raise EngineeringPromotionError(
            "writer attempt has no candidate root"
        )
    main, candidate = _safe_roots(
        main_root,
        Path(candidate_value),
    )

    promotion_id = "epr_" + uuid4().hex
    created_at = utc_now()

    authorized, authorization_code, fresh = (
        _fresh_authorization_state(
            main,
            writer_attempt,
            evaluator_report,
            authorization_report,
        )
    )

    transaction = _prepare_transaction_root(
        transaction_root,
        main,
        candidate,
    )

    supplied_auth = authorization_report.get(
        "authorization"
    )
    supplied_fingerprint = (
        supplied_auth.get("fingerprint")
        if isinstance(supplied_auth, Mapping)
        else None
    )

    if not authorized or not isinstance(fresh, Mapping):
        report: dict[str, object] = {
            "schema":
                "sira.engineering_promotion_report.v1",
            "policy_version":
                ENGINEERING_PROMOTION_POLICY_VERSION,
            "promotion_id": promotion_id,
            "created_at": created_at,
            "status": "denied",
            "decision_code": authorization_code,
            "promotion_performed": False,
            "rollback_performed": False,
            "rollback_verified": False,
            "authorization_fingerprint":
                supplied_fingerprint,
            "changed_files": [],
            "pre_source_sha256": None,
            "target_source_sha256": None,
            "final_source_sha256": None,
            "post_verification": None,
            "package_installation_performed": False,
            "main_tree_command_execution": False,
            "requires_manual_recovery": False,
        }
        _persist_report(
            transaction,
            report,
        )
        return report

    binding = fresh.get("binding")
    fresh_auth = fresh.get("authorization")
    if not (
        isinstance(binding, Mapping)
        and isinstance(fresh_auth, Mapping)
    ):
        raise EngineeringPromotionError(
            "fresh authorization is missing protected binding"
        )

    changed_files = binding.get(
        "changed_files"
    )
    changed_hashes = binding.get(
        "changed_file_sha256"
    )
    pre_source_sha = binding.get(
        "main_source_sha256"
    )
    target_source_sha = binding.get(
        "candidate_source_sha256"
    )
    fingerprint = fresh_auth.get(
        "fingerprint"
    )

    if (
        not isinstance(changed_files, list)
        or not changed_files
        or len(changed_files)
        > MAX_PROMOTION_FILES
        or len(set(changed_files))
        != len(changed_files)
        or not isinstance(changed_hashes, Mapping)
        or not isinstance(pre_source_sha, str)
        or len(pre_source_sha) != 64
        or not isinstance(target_source_sha, str)
        or len(target_source_sha) != 64
        or not isinstance(fingerprint, str)
        or len(fingerprint) != 64
    ):
        raise EngineeringPromotionError(
            "fresh authorization binding is malformed"
        )

    verification_plan = writer_attempt.get(
        "verification_plan"
    )
    if not isinstance(verification_plan, Mapping):
        raise EngineeringPromotionError(
            "writer attempt has no engineering verification plan"
        )

    current_main_sha, _ = _surface_snapshot(main)
    current_candidate_sha, _ = _surface_snapshot(
        candidate
    )
    if current_main_sha != pre_source_sha:
        authorization_code = "stale_authorization"
        authorized = False
    if current_candidate_sha != target_source_sha:
        authorization_code = "stale_authorization"
        authorized = False

    if not authorized:
        report = {
            "schema":
                "sira.engineering_promotion_report.v1",
            "policy_version":
                ENGINEERING_PROMOTION_POLICY_VERSION,
            "promotion_id": promotion_id,
            "created_at": created_at,
            "status": "denied",
            "decision_code": authorization_code,
            "promotion_performed": False,
            "rollback_performed": False,
            "rollback_verified": False,
            "authorization_fingerprint":
                fingerprint,
            "changed_files": [],
            "pre_source_sha256":
                current_main_sha,
            "target_source_sha256":
                target_source_sha,
            "final_source_sha256":
                current_main_sha,
            "post_verification": None,
            "package_installation_performed": False,
            "main_tree_command_execution": False,
            "requires_manual_recovery": False,
        }
        _persist_report(
            transaction,
            report,
        )
        return report

    sources: list[
        tuple[str, bytes, Path]
    ] = []
    total_bytes = 0
    for relative in changed_files:
        if not isinstance(relative, str):
            raise EngineeringPromotionError(
                "authorization changed-file path is invalid"
            )
        digest = changed_hashes.get(
            relative
        )
        if (
            not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise EngineeringPromotionError(
                "authorization changed-file hash is invalid"
            )
        _, payload = _safe_candidate_file(
            candidate,
            relative,
            digest,
        )
        total_bytes += len(payload)
        if (
            total_bytes
            > MAX_PROMOTION_TOTAL_BYTES
        ):
            raise EngineeringPromotionError(
                "engineering promotion batch exceeds byte limit"
            )
        target = _safe_main_target(
            main,
            relative,
        )
        sources.append(
            (relative, payload, target)
        )

    backup_root = transaction / "backup"
    entries = create_backup(
        main,
        backup_root,
        changed_files,
    )

    write_error: str | None = None
    structure_error: str | None = None
    verification_error: str | None = None
    verification: Mapping[str, object] | None = None

    try:
        for _, payload, target in sources:
            _atomic_replace(
                target,
                payload,
            )
    except Exception as exc:
        write_error = type(exc).__name__

    structure_ok = False
    if write_error is None:
        try:
            promoted_sha, _ = _surface_snapshot(
                main
            )
            candidate_sha_after, _ = (
                _surface_snapshot(candidate)
            )
            structure_ok = bool(
                promoted_sha
                == target_source_sha
                and candidate_sha_after
                == target_source_sha
            )
            if not structure_ok:
                structure_error = (
                    "source_surface_mismatch"
                )
        except Exception as exc:
            structure_error = (
                type(exc).__name__
            )

    postverify_root = (
        transaction
        / "postverify_candidate"
    )
    if structure_ok:
        try:
            _copy_promoted_surface(
                main,
                postverify_root,
            )
            post_copy_sha, _ = _surface_snapshot(
                postverify_root
            )
            if (
                post_copy_sha
                != target_source_sha
            ):
                raise EngineeringPromotionError(
                    "post-verification copy checksum mismatch"
                )
            raw_verification = (
                execute_verification_plan(
                    postverify_root,
                    verification_plan,
                    main_root=main,
                    command_runner=command_runner,
                    bubblewrap_path=bubblewrap_path,
                )
            )
            verification = raw_verification
        except Exception as exc:
            verification_error = (
                type(exc).__name__
            )

    verified = False
    final_promoted_sha: str | None = None
    if (
        structure_ok
        and verification_error is None
        and _verification_passed(
            verification
        )
    ):
        try:
            final_promoted_sha, _ = (
                _surface_snapshot(main)
            )
            postverify_sha, _ = (
                _surface_snapshot(
                    postverify_root
                )
            )
            candidate_final_sha, _ = (
                _surface_snapshot(candidate)
            )
            verified = bool(
                final_promoted_sha
                == target_source_sha
                and postverify_sha
                == target_source_sha
                and candidate_final_sha
                == target_source_sha
            )
        except Exception as exc:
            verification_error = (
                type(exc).__name__
            )

    if verified:
        report = {
            "schema":
                "sira.engineering_promotion_report.v1",
            "policy_version":
                ENGINEERING_PROMOTION_POLICY_VERSION,
            "promotion_id": promotion_id,
            "created_at": created_at,
            "status": "promoted",
            "decision_code":
                "engineering_promotion_committed",
            "promotion_performed": True,
            "rollback_performed": False,
            "rollback_verified": False,
            "authorization_fingerprint":
                fingerprint,
            "changed_files":
                list(changed_files),
            "pre_source_sha256":
                pre_source_sha,
            "target_source_sha256":
                target_source_sha,
            "final_source_sha256":
                final_promoted_sha,
            "post_verification":
                _verification_summary(
                    verification
                ),
            "post_verification_isolated":
                True,
            "post_verification_source_sha256":
                target_source_sha,
            "package_installation_performed":
                False,
            "main_tree_command_execution":
                False,
            "requires_manual_recovery":
                False,
        }
        _cleanup_transaction_artifacts(
            transaction,
            keep_backup=False,
        )
        _persist_report(
            transaction,
            report,
        )
        return report

    rollback_verified, rollback_error = (
        _restore_and_verify(
            main,
            backup_root,
            entries,
            pre_source_sha,
        )
    )

    if write_error is not None:
        decision_code = (
            "engineering_promotion_write_failed"
        )
    elif not structure_ok:
        decision_code = (
            "engineering_promotion_structure_failed"
        )
    elif verification_error is not None:
        decision_code = (
            "engineering_post_verification_error"
        )
    else:
        decision_code = (
            "engineering_post_verification_failed"
        )

    status = (
        "rolled_back"
        if rollback_verified
        else "rollback_failed"
    )
    try:
        final_source_sha, _ = (
            _surface_snapshot(main)
        )
    except Exception:
        final_source_sha = None

    report = {
        "schema":
            "sira.engineering_promotion_report.v1",
        "policy_version":
            ENGINEERING_PROMOTION_POLICY_VERSION,
        "promotion_id": promotion_id,
        "created_at": created_at,
        "status": status,
        "decision_code": (
            decision_code
            if rollback_verified
            else "engineering_rollback_incomplete"
        ),
        "promotion_performed": False,
        "rollback_performed": True,
        "rollback_verified":
            rollback_verified,
        "authorization_fingerprint":
            fingerprint,
        "changed_files":
            list(changed_files),
        "pre_source_sha256":
            pre_source_sha,
        "target_source_sha256":
            target_source_sha,
        "final_source_sha256":
            final_source_sha,
        "post_verification":
            _verification_summary(
                verification
            ),
        "post_verification_isolated":
            structure_ok,
        "post_verification_source_sha256":
            target_source_sha
            if structure_ok
            else None,
        "write_error": write_error,
        "structure_error": structure_error,
        "verification_error":
            verification_error,
        "rollback_error": rollback_error,
        "package_installation_performed":
            False,
        "main_tree_command_execution":
            False,
        "requires_manual_recovery":
            not rollback_verified,
    }

    _cleanup_transaction_artifacts(
        transaction,
        keep_backup=not rollback_verified,
    )
    _persist_report(
        transaction,
        report,
    )
    return report
