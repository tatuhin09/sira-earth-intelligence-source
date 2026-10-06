"""Protected checksum authorization gate for engineering candidates.

This module is part of SIRA's protected shell. It independently rebinds a
v1.8E writer attempt and v1.8F evaluator recommendation to the exact current
main engineering surface, exact candidate surface, declared source-file hashes,
and clean isolated verification.

It authorizes no writes by itself. A successful result is checksum-only input
for a future protected transactional engineering promotion step.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from .models import utc_now
from .self_modification import PROTECTED_PATHS

ENGINEERING_AUTHORIZATION_POLICY_VERSION = 1
SUPPORTED_ENGINEERING_EVALUATOR_POLICIES = frozenset({1})

MAX_EDIT_FILES = 12
MAX_EDIT_FILE_BYTES = 256 * 1024
MAX_EDIT_TOTAL_BYTES = 768 * 1024

MAX_COPY_FILES = 5000
MAX_COPY_FILE_BYTES = 1024 * 1024
MAX_COPY_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ANOMALY_PATHS = 40

_SUPPORTED_SUFFIXES = frozenset({
    ".py",
    ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx",
    ".dart",
    ".java",
    ".c", ".h",
    ".cc", ".cpp", ".cxx", ".hh", ".hpp",
    ".rs",
    ".go",
    ".sql",
})

# Independent copy-only policy. Do not add these to _SUPPORTED_SUFFIXES:
# that set also controls which files a candidate may edit.
_VERIFICATION_SUPPORT_SUFFIXES = frozenset({
    ".json", ".html", ".css", ".svg", ".md", ".txt",
    ".toml", ".yaml", ".yml",
})

_EDITABLE_TOP_LEVEL_ROOTS = frozenset({
    "src",
    "app",
    "lib",
    "test",
    "tests",
    "include",
    "cmd",
    "internal",
    "pkg",
    "packages",
    "server",
    "client",
    "web",
    "api",
    "database",
    "db",
    "migrations",
    "sql",
})

_COPY_MANIFESTS = frozenset({
    ".gitignore",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "pytest.ini",
    "tox.ini",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "tsconfig.json",
    "pubspec.yaml",
    "pubspec.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradlew",
    "mvnw",
    "CMakeLists.txt",
    "Makefile",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    ".sqlfluff",
    "sqlfluff.toml",
})

_IGNORED_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".cache",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "coverage",
    ".next",
    ".dart_tool",
    ".gradle",
    "target",
    ".sira-build",
    "runs",
    "memory",
    "runtime",
    "improvements",
    "credentials",
    "secrets",
})

_SENSITIVE_EXACT_NAMES = frozenset({
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "service-account.json",
})
_SENSITIVE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _json_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_sensitive_name(name: str) -> bool:
    lower = name.casefold()
    return (
        lower in _SENSITIVE_EXACT_NAMES
        or lower.endswith(_SENSITIVE_SUFFIXES)
    )


def _safe_roots(
    main_root: Path,
    candidate_root: Path,
) -> tuple[Path, Path]:
    main = Path(main_root).resolve()
    raw_candidate = Path(candidate_root)
    if raw_candidate.is_symlink():
        raise ValueError(
            "engineering authorization candidate root must not be a symlink"
        )
    candidate = raw_candidate.resolve()
    if not main.is_dir() or not candidate.is_dir():
        raise ValueError(
            "engineering authorization roots must exist"
        )
    if candidate == main:
        raise ValueError(
            "engineering candidate cannot equal the main project"
        )
    try:
        candidate.relative_to(main)
    except ValueError:
        return main, candidate
    raise ValueError(
        "engineering candidate must be outside the main project"
    )


def _copyable_relative(relative: PurePosixPath) -> bool:
    if not relative.parts:
        return False
    if any(
        part in _IGNORED_DIRS
        or part.startswith(".")
        for part in relative.parts[:-1]
    ):
        return False
    if _is_sensitive_name(relative.name):
        return False
    if (
        len(relative.parts) == 1
        and relative.name in _COPY_MANIFESTS
    ):
        return True
    return relative.suffix.casefold() in (
        _SUPPORTED_SUFFIXES | _VERIFICATION_SUPPORT_SUFFIXES
    )


def _surface_snapshot(
    root: Path,
) -> tuple[str, dict[str, dict[str, object]]]:
    """Replicate the bounded v1.8D source-copy surface independently."""
    root = Path(root).resolve()
    digest = hashlib.sha256()
    files: dict[str, dict[str, object]] = {}
    count = 0
    total = 0

    for current, dirs, names in os.walk(
        root,
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
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = PurePosixPath(
                path.relative_to(root).as_posix()
            )
            if not _copyable_relative(relative):
                continue
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise ValueError(
                    "engineering source surface became unreadable"
                ) from exc

            size = len(payload)
            if size > MAX_COPY_FILE_BYTES:
                continue
            count += 1
            total += size
            if count > MAX_COPY_FILES:
                raise ValueError(
                    "engineering source surface exceeds file limit"
                )
            if total > MAX_COPY_TOTAL_BYTES:
                raise ValueError(
                    "engineering source surface exceeds byte limit"
                )

            relative_text = relative.as_posix()
            file_sha = hashlib.sha256(payload).hexdigest()
            utf8 = True
            if b"\x00" in payload:
                utf8 = False
            else:
                try:
                    payload.decode("utf-8")
                except UnicodeDecodeError:
                    utf8 = False

            digest.update(
                relative_text.encode("utf-8") + b"\0"
            )
            digest.update(payload)
            digest.update(b"\0")
            files[relative_text] = {
                "sha256": file_sha,
                "size": size,
                "utf8": utf8,
            }

    return digest.hexdigest(), files


def _surface_anomalies(
    candidate_root: Path,
) -> dict[str, list[str]]:
    candidate_root = Path(candidate_root).resolve()
    symlinks: list[str] = []
    sensitive: list[str] = []
    unexpected: list[str] = []

    for current, dirs, names in os.walk(
        candidate_root,
        followlinks=False,
    ):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            child = current_path / name
            relative = child.relative_to(
                candidate_root
            ).as_posix()
            if child.is_symlink():
                symlinks.append(relative)
                continue
            if (
                name in _IGNORED_DIRS
                or name.startswith(".")
            ):
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs

        for name in sorted(names):
            child = current_path / name
            relative = child.relative_to(
                candidate_root
            ).as_posix()
            if child.is_symlink():
                symlinks.append(relative)
                continue
            if not child.is_file():
                unexpected.append(relative)
                continue
            if _is_sensitive_name(name):
                sensitive.append(relative)
                continue

            rel = PurePosixPath(relative)
            if (
                len(rel.parts) == 1
                and name in _COPY_MANIFESTS
            ):
                continue
            if (
                rel.suffix.casefold()
                in (_SUPPORTED_SUFFIXES | _VERIFICATION_SUPPORT_SUFFIXES)
            ):
                continue
            if name.startswith("."):
                continue
            unexpected.append(relative)

    return {
        "symlink_paths":
            sorted(symlinks)[:MAX_ANOMALY_PATHS],
        "sensitive_paths":
            sorted(sensitive)[:MAX_ANOMALY_PATHS],
        "unexpected_paths":
            sorted(unexpected)[:MAX_ANOMALY_PATHS],
    }


def _safe_declared_path(relative: str) -> bool:
    if (
        not isinstance(relative, str)
        or not relative
        or len(relative) > 320
        or "\\" in relative
    ):
        return False
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or any(
            part in {"", ".", ".."}
            for part in path.parts
        )
    ):
        return False

    normalized = path.as_posix()
    if normalized in PROTECTED_PATHS:
        return False
    if any(
        part in _IGNORED_DIRS
        or part.startswith(".")
        for part in path.parts[:-1]
    ):
        return False
    if _is_sensitive_name(path.name):
        return False
    if path.suffix.casefold() not in _SUPPORTED_SUFFIXES:
        return False
    if (
        len(path.parts) > 1
        and path.parts[0]
        not in _EDITABLE_TOP_LEVEL_ROOTS
    ):
        return False
    return True


def _verification_is_clean(
    verification: Mapping[str, object] | None,
    writer_attempt: Mapping[str, object],
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if not isinstance(verification, Mapping):
        return False, ["verification_missing"]

    required = {
        "schema":
            "sira.engineering_verification_result.v1",
        "status": "completed",
        "outcome": "verification_passed",
        "overall_passed": True,
        "authority_granted": False,
        "promotion_authorized": False,
        "raw_output_included": False,
    }
    for key, expected in required.items():
        if verification.get(key) != expected:
            failures.append(
                f"verification_{key}_invalid"
            )

    if (
        writer_attempt.get("verification_executed")
        is not True
    ):
        failures.append(
            "verification_not_executed"
        )

    processes = verification.get(
        "processes_executed"
    )
    if type(processes) is not int or processes < 1:
        failures.append(
            "verification_process_count_invalid"
        )

    missing = verification.get(
        "missing_command_ids"
    )
    if not isinstance(missing, list) or missing:
        failures.append(
            "verification_commands_missing"
        )

    diagnostic_count = verification.get(
        "diagnostic_count"
    )
    if (
        type(diagnostic_count) is not int
        or diagnostic_count != 0
    ):
        failures.append(
            "verification_diagnostics_not_clean"
        )

    commands = verification.get("commands")
    if not isinstance(commands, list) or not commands:
        failures.append(
            "verification_commands_invalid"
        )
    else:
        for row in commands:
            if (
                not isinstance(row, Mapping)
                or row.get("status") != "passed"
                or row.get("returncode") != 0
                or row.get("timed_out") is not False
                or row.get(
                    "output_limit_exceeded"
                ) is not False
            ):
                failures.append(
                    "verification_command_failed"
                )
                break

    return (
        not failures,
        sorted(set(failures)),
    )


def _diagnostic_advisory_is_clean(
    value: object,
) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("schema")
        == "sira.engineering_diagnostic_advisory.v1"
        and value.get("diagnostic_count") == 0
        and value.get("repair_authority")
        == "advisory_only"
        and value.get("raw_output_included")
        is False
        and value.get("authority_granted")
        is False
        and value.get("promotion_authorized")
        is False
    )


def _evaluator_report_is_valid(
    report: Mapping[str, object],
) -> bool:
    checks = report.get("checks")
    risk_flags = report.get("risk_flags")
    return bool(
        report.get("schema")
        == "sira.engineering_evaluator_gate.v1"
        and report.get("policy_version")
        in SUPPORTED_ENGINEERING_EVALUATOR_POLICIES
        and report.get("decision") == "accept"
        and report.get("decision_code")
        == "engineering_candidate_verified"
        and report.get("recommendation")
        == "eligible_for_protected_gate"
        and report.get(
            "promotion_candidate_eligible"
        )
        is True
        and report.get("promotion_authorized")
        is False
        and report.get("promotion_performed")
        is False
        and report.get("authority_granted")
        is False
        and report.get("main_tree_modified")
        is False
        and isinstance(checks, Mapping)
        and bool(checks)
        and all(
            value is True
            for value in checks.values()
        )
        and isinstance(risk_flags, list)
        and not risk_flags
    )


def evaluate_engineering_authorization(
    main_root: Path,
    writer_attempt: Mapping[str, object],
    evaluator_report: Mapping[str, object],
) -> dict[str, object]:
    """Issue a protected checksum-only engineering authorization decision."""
    if not isinstance(writer_attempt, Mapping):
        raise ValueError(
            "engineering writer attempt must be a mapping"
        )
    if not isinstance(evaluator_report, Mapping):
        raise ValueError(
            "engineering evaluator report must be a mapping"
        )

    candidate_value = writer_attempt.get(
        "candidate_root"
    )
    if (
        not isinstance(candidate_value, str)
        or not candidate_value
    ):
        raise ValueError(
            "engineering writer attempt has no candidate root"
        )

    main_root, candidate_root = _safe_roots(
        main_root,
        Path(candidate_value),
    )

    failures: set[str] = set()

    evaluator_valid = _evaluator_report_is_valid(
        evaluator_report
    )
    if not evaluator_valid:
        failures.add(
            "engineering_evaluator_not_eligible"
        )

    attempt_valid = bool(
        writer_attempt.get("schema")
        == "sira.engineering_writer_attempt.v1"
        and writer_attempt.get("status")
        == "verified_candidate_ready"
        and writer_attempt.get(
            "candidate_root"
        )
        == str(candidate_root)
        and writer_attempt.get(
            "main_tree_modified"
        )
        is False
        and writer_attempt.get(
            "package_installation_performed"
        )
        is False
        and writer_attempt.get(
            "authority_granted"
        )
        is False
        and writer_attempt.get(
            "promotion_performed"
        )
        is False
        and writer_attempt.get(
            "promotion_authorized"
        )
        is False
    )
    if not attempt_valid:
        failures.add(
            "writer_attempt_invalid"
        )

    edit_candidate = writer_attempt.get(
        "edit_candidate"
    )
    manifest = (
        edit_candidate.get(
            "candidate_manifest"
        )
        if isinstance(edit_candidate, Mapping)
        else None
    )
    edit_report = (
        edit_candidate.get("edit_report")
        if isinstance(edit_candidate, Mapping)
        else None
    )
    edit_policy = (
        edit_candidate.get("policy")
        if isinstance(edit_candidate, Mapping)
        else None
    )

    edit_candidate_valid = bool(
        isinstance(edit_candidate, Mapping)
        and edit_candidate.get("schema")
        == "sira.engineering_edit_candidate.v1"
        and edit_candidate.get("status")
        == "candidate_prepared"
        and edit_candidate.get("source_root")
        == str(main_root)
        and edit_candidate.get(
            "candidate_root"
        )
        == str(candidate_root)
        and edit_candidate.get(
            "main_tree_modified"
        )
        is False
        and edit_candidate.get(
            "package_installation_performed"
        )
        is False
        and edit_candidate.get(
            "promotion_authorized"
        )
        is False
    )
    if not edit_candidate_valid:
        failures.add(
            "edit_candidate_invalid"
        )

    manifest_valid = bool(
        isinstance(manifest, Mapping)
        and manifest.get("schema")
        == "sira.engineering_candidate_manifest.v1"
        and manifest.get("source_root")
        == str(main_root)
        and manifest.get(
            "candidate_root"
        )
        == str(candidate_root)
        and isinstance(
            manifest.get(
                "source_copy_sha256"
            ),
            str,
        )
        and _SHA256_RE.fullmatch(
            str(
                manifest.get(
                    "source_copy_sha256"
                )
            )
        )
        is not None
        and manifest.get(
            "symlinks_copied"
        )
        is False
        and manifest.get(
            "sensitive_artifact_patterns_excluded"
        )
        is True
        and manifest.get(
            "vendor_dependency_trees_excluded"
        )
        is True
        and manifest.get(
            "main_tree_modified"
        )
        is False
        and manifest.get(
            "package_installation_performed"
        )
        is False
        and manifest.get(
            "promotion_allowed"
        )
        is False
        and manifest.get(
            "authority_granted"
        )
        is False
    )
    if not manifest_valid:
        failures.add(
            "candidate_manifest_invalid"
        )

    edit_report_valid = bool(
        isinstance(edit_report, Mapping)
        and edit_report.get("schema")
        == "sira.engineering_edit_result.v1"
        and edit_report.get(
            "candidate_only"
        )
        is True
        and edit_report.get(
            "verification_required"
        )
        is True
        and edit_report.get(
            "main_tree_modified"
        )
        is False
        and edit_report.get(
            "package_installation_performed"
        )
        is False
        and edit_report.get(
            "authority_granted"
        )
        is False
        and edit_report.get(
            "promotion_performed"
        )
        is False
        and edit_report.get(
            "promotion_authorized"
        )
        is False
    )
    if not edit_report_valid:
        failures.add(
            "edit_report_invalid"
        )

    policy_valid = bool(
        isinstance(edit_policy, Mapping)
        and edit_policy.get("schema")
        == "sira.engineering_edit_policy.v1"
        and edit_policy.get("project_root")
        == str(main_root)
        and edit_policy.get("status")
        == "ready"
        and edit_policy.get(
            "candidate_only"
        )
        is True
        and edit_policy.get(
            "verification_required"
        )
        is True
        and edit_policy.get(
            "dependency_manifest_editing_allowed"
        )
        is False
        and edit_policy.get(
            "package_installation_allowed"
        )
        is False
        and edit_policy.get(
            "main_tree_editing_allowed"
        )
        is False
        and edit_policy.get(
            "authority_granted"
        )
        is False
        and edit_policy.get(
            "promotion_authorized"
        )
        is False
    )
    if not policy_valid:
        failures.add(
            "edit_policy_invalid"
        )

    writer_report = writer_attempt.get(
        "writer_report"
    )
    writer_report_valid = bool(
        isinstance(writer_report, Mapping)
        and writer_report.get("schema")
        == "sira.engineering_writer_report.v1"
        and writer_report.get(
            "candidate_only"
        )
        is True
        and writer_report.get(
            "main_tree_modified"
        )
        is False
        and writer_report.get(
            "promotion_authorized"
        )
        is False
    )
    if not writer_report_valid:
        failures.add(
            "writer_report_invalid"
        )

    verification = writer_attempt.get(
        "verification"
    )
    verification_clean, verification_failures = (
        _verification_is_clean(
            verification
            if isinstance(
                verification,
                Mapping,
            )
            else None,
            writer_attempt,
        )
    )
    failures.update(
        verification_failures
    )

    advisory_clean = (
        _diagnostic_advisory_is_clean(
            writer_attempt.get(
                "diagnostic_advisory"
            )
        )
    )
    if not advisory_clean:
        failures.add(
            "diagnostic_advisory_not_clean"
        )

    try:
        main_surface_sha, main_files = (
            _surface_snapshot(main_root)
        )
        candidate_surface_sha, candidate_files = (
            _surface_snapshot(candidate_root)
        )
    except ValueError:
        main_surface_sha = None
        candidate_surface_sha = None
        main_files = {}
        candidate_files = {}
        failures.add(
            "surface_snapshot_failed"
        )

    source_sha = (
        manifest.get(
            "source_copy_sha256"
        )
        if isinstance(manifest, Mapping)
        else None
    )
    base_current = bool(
        isinstance(main_surface_sha, str)
        and isinstance(source_sha, str)
        and main_surface_sha == source_sha
    )
    if not base_current:
        failures.add(
            "stale_candidate_base"
        )

    anomalies = _surface_anomalies(
        candidate_root
    )
    if anomalies["symlink_paths"]:
        failures.add("symlink_present")
    if anomalies["sensitive_paths"]:
        failures.add(
            "sensitive_artifact_present"
        )
    if anomalies["unexpected_paths"]:
        failures.add(
            "unexpected_candidate_file"
        )

    declared: list[str] = []
    declared_hashes: dict[str, str] = {}
    if edit_report_valid:
        raw_changed = edit_report.get(
            "files_changed"
        )
        raw_hashes = edit_report.get(
            "file_sha256"
        )
        if (
            isinstance(raw_changed, list)
            and raw_changed
            and len(raw_changed)
            <= MAX_EDIT_FILES
            and len(set(raw_changed))
            == len(raw_changed)
            and isinstance(
                raw_hashes,
                Mapping,
            )
        ):
            for relative in raw_changed:
                if (
                    not isinstance(
                        relative,
                        str,
                    )
                    or not _safe_declared_path(
                        relative
                    )
                ):
                    failures.add(
                        "declared_edit_invalid"
                    )
                    continue
                digest = raw_hashes.get(
                    relative
                )
                if (
                    not isinstance(
                        digest,
                        str,
                    )
                    or _SHA256_RE.fullmatch(
                        digest
                    )
                    is None
                ):
                    failures.add(
                        "declared_edit_invalid"
                    )
                    continue
                declared.append(
                    relative
                )
                declared_hashes[
                    relative
                ] = digest
        else:
            failures.add(
                "declared_edit_invalid"
            )

    declared = sorted(set(declared))
    main_paths = set(main_files)
    candidate_paths = set(
        candidate_files
    )
    added = sorted(
        candidate_paths - main_paths
    )
    removed = sorted(
        main_paths - candidate_paths
    )
    modified = sorted(
        relative
        for relative
        in (
            main_paths
            & candidate_paths
        )
        if (
            main_files[relative][
                "sha256"
            ]
            != candidate_files[
                relative
            ]["sha256"]
        )
    )
    actual_changed = sorted(
        set(added)
        | set(removed)
        | set(modified)
    )

    if removed:
        failures.add("file_removed")
    if actual_changed != declared:
        failures.add(
            "changed_set_mismatch"
        )

    changed_bytes = 0
    candidate_hashes_match = bool(
        declared
    )
    changed_text_only = True
    changed_files_bounded = True

    for relative in declared:
        metadata = candidate_files.get(
            relative
        )
        if not isinstance(
            metadata,
            Mapping,
        ):
            candidate_hashes_match = False
            changed_text_only = False
            changed_files_bounded = False
            continue

        if (
            metadata.get("sha256")
            != declared_hashes.get(
                relative
            )
        ):
            candidate_hashes_match = False

        if metadata.get("utf8") is not True:
            changed_text_only = False

        size = metadata.get("size")
        if type(size) is not int:
            changed_files_bounded = False
            continue
        changed_bytes += size
        if size > MAX_EDIT_FILE_BYTES:
            changed_files_bounded = False

    if not candidate_hashes_match:
        failures.add(
            "candidate_hash_mismatch"
        )
    if not changed_text_only:
        failures.add(
            "non_utf8_change"
        )
    if (
        not changed_files_bounded
        or changed_bytes
        > MAX_EDIT_TOTAL_BYTES
    ):
        failures.add(
            "changed_bytes_exceeded"
        )

    evaluator_binding_ok = False
    if evaluator_valid:
        binding = evaluator_report.get(
            "source_binding"
        )
        diff = evaluator_report.get(
            "diff"
        )
        evaluator_binding_ok = bool(
            isinstance(binding, Mapping)
            and binding.get(
                "manifest_source_copy_sha256"
            )
            == source_sha
            and binding.get(
                "current_source_copy_sha256"
            )
            == main_surface_sha
            and binding.get(
                "candidate_base_current"
            )
            is True
            and isinstance(diff, Mapping)
            and diff.get(
                "declared_changed_files"
            )
            == declared
            and diff.get(
                "actual_changed_files"
            )
            == actual_changed
            and diff.get(
                "removed_files"
            )
            == []
            and diff.get(
                "symlink_paths"
            )
            == []
            and diff.get(
                "sensitive_paths"
            )
            == []
            and diff.get(
                "unexpected_paths"
            )
            == []
        )
        if not evaluator_binding_ok:
            failures.add(
                "engineering_evaluator_binding_mismatch"
            )

    structural_failures = {
        "writer_attempt_invalid",
        "edit_candidate_invalid",
        "candidate_manifest_invalid",
        "edit_report_invalid",
        "edit_policy_invalid",
        "writer_report_invalid",
        "surface_snapshot_failed",
        "declared_edit_invalid",
        "symlink_present",
        "sensitive_artifact_present",
        "unexpected_candidate_file",
        "file_removed",
        "changed_set_mismatch",
        "candidate_hash_mismatch",
        "non_utf8_change",
        "changed_bytes_exceeded",
        "engineering_evaluator_binding_mismatch",
    }

    if (
        "stale_candidate_base"
        in failures
    ):
        decision = "deny"
        code = "stale_candidate_base"
    elif not evaluator_valid:
        decision = "deny"
        code = (
            "engineering_evaluator_not_eligible"
        )
    elif failures & structural_failures:
        decision = "deny"
        code = (
            "engineering_candidate_integrity_failed"
        )
    elif (
        not verification_clean
        or not advisory_clean
    ):
        decision = "deny"
        code = (
            "engineering_verification_not_clean"
        )
    elif not actual_changed:
        decision = "deny"
        code = "no_effective_change"
    else:
        decision = "allow"
        code = (
            "engineering_promotion_authorized"
        )

    evaluator_sha = _json_digest(
        evaluator_report
    )
    writer_attempt_sha = _json_digest(
        writer_attempt
    )
    verification_sha = (
        _json_digest(verification)
        if isinstance(
            verification,
            Mapping,
        )
        else None
    )
    changed_binding_sha = _json_digest({
        "changed_files": declared,
        "file_sha256": {
            key: declared_hashes[key]
            for key in declared
            if key in declared_hashes
        },
    })

    fingerprint_material = {
        "engineering_authorization_policy_version":
            ENGINEERING_AUTHORIZATION_POLICY_VERSION,
        "engineering_evaluator_report_sha256":
            evaluator_sha,
        "engineering_writer_attempt_sha256":
            writer_attempt_sha,
        "main_source_sha256":
            main_surface_sha,
        "candidate_source_sha256":
            candidate_surface_sha,
        "changed_files_sha256":
            changed_binding_sha,
        "verification_sha256":
            verification_sha,
    }
    fingerprint = _json_digest(
        fingerprint_material
    )

    return {
        "schema":
            "sira.engineering_authorization_gate.v1",
        "policy_version":
            ENGINEERING_AUTHORIZATION_POLICY_VERSION,
        "created_at": utc_now(),
        "decision": decision,
        "decision_code": code,
        "promotion_allowed":
            decision == "allow",
        "promotion_performed": False,
        "authority_scope":
            "engineering_candidate_checksum_only",
        "checks": {
            "engineering_evaluator_eligible":
                evaluator_valid,
            "writer_attempt_valid":
                attempt_valid,
            "edit_candidate_valid":
                edit_candidate_valid,
            "candidate_manifest_valid":
                manifest_valid,
            "edit_report_valid":
                edit_report_valid,
            "edit_policy_valid":
                policy_valid,
            "writer_report_valid":
                writer_report_valid,
            "candidate_base_current":
                base_current,
            "candidate_surface_safe":
                not any(
                    anomalies[key]
                    for key in (
                        "symlink_paths",
                        "sensitive_paths",
                        "unexpected_paths",
                    )
                ),
            "exact_declared_diff":
                actual_changed == declared
                and not removed,
            "candidate_hashes_match":
                candidate_hashes_match,
            "text_only":
                changed_text_only,
            "bounded_diff":
                changed_files_bounded
                and changed_bytes
                <= MAX_EDIT_TOTAL_BYTES,
            "verification_clean":
                verification_clean,
            "diagnostic_advisory_clean":
                advisory_clean,
            "evaluator_binding_match":
                evaluator_binding_ok,
        },
        "failures": sorted(failures),
        "binding": {
            "source_manifest_sha256":
                source_sha,
            "main_source_sha256":
                main_surface_sha,
            "candidate_source_sha256":
                candidate_surface_sha,
            "changed_files":
                declared,
            "changed_file_sha256": {
                key: declared_hashes[key]
                for key in declared
                if key
                in declared_hashes
            },
            "changed_bytes":
                changed_bytes,
        },
        "authorization": {
            **fingerprint_material,
            "fingerprint":
                fingerprint,
            "checksum_only": True,
            "write_authority": False,
            "promotion_execution_authority":
                False,
        },
    }
