"""Multi-language candidate editing foundation.

This module is deliberately parallel to SIRA's protected self-modification
path. It prepares bounded candidate copies for supported engineering projects
and applies bounded full-text edits inside those candidates only.

It does not alter SIRA's MODIFIABLE_ROOTS, writer, evaluators, promotion path,
runtime, package state, credentials, or main project tree.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
from typing import Any, Mapping

from .engineering_intelligence import (
    inspect_project,
    language_for_path,
    plan_verification,
)
from .models import utc_now
from .self_modification import PROTECTED_PATHS
from .storage import write_json

ENGINEERING_EDIT_POLICY_VERSION = 1

MAX_EDIT_FILES = 12
MAX_EDIT_FILE_BYTES = 256 * 1024
MAX_EDIT_TOTAL_BYTES = 768 * 1024

MAX_COPY_FILES = 5000
MAX_COPY_FILE_BYTES = 1024 * 1024
MAX_COPY_TOTAL_BYTES = 32 * 1024 * 1024

SUPPORTED_EDIT_SUFFIXES = frozenset({
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

# Verification may require public fixtures, documentation, and desktop assets
# that are not model-editable source. Keep this separate from edit authority.
VERIFICATION_SUPPORT_SUFFIXES = frozenset({
    ".json",
    ".html",
    ".css",
    ".svg",
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
})

EDITABLE_TOP_LEVEL_ROOTS = frozenset({
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

COPY_MANIFESTS = frozenset({
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

IGNORED_COPY_DIRS = frozenset({
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

DENIED_EDIT_NAMES = frozenset({
    "setup.py",
    "conanfile.py",
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


class EngineeringCandidateEditError(ValueError):
    """Raised when an engineering candidate edit violates the boundary."""


def _detected_languages(profile: Mapping[str, Any]) -> set[str]:
    rows = profile.get("languages")
    rows = rows if isinstance(rows, list) else []
    return {
        str(row.get("language"))
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("language"), str)
    }


def _expanded_language_family(languages: set[str]) -> set[str]:
    expanded = set(languages)
    if expanded & {"javascript", "typescript"}:
        expanded.update({"javascript", "typescript"})
    if expanded & {"c", "cpp"}:
        expanded.update({"c", "cpp"})
    return expanded


def build_engineering_edit_policy(root: Path) -> dict[str, Any]:
    """Build a read-only edit policy from the locally detected project stack."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise EngineeringCandidateEditError("project root must exist")

    profile = inspect_project(root)
    detected = _detected_languages(profile)
    editable_languages = _expanded_language_family(detected)

    editable_suffixes = sorted(
        suffix
        for suffix in SUPPORTED_EDIT_SUFFIXES
        if language_for_path("candidate" + suffix) in editable_languages
    )

    return {
        "schema": "sira.engineering_edit_policy.v1",
        "policy_version": ENGINEERING_EDIT_POLICY_VERSION,
        "created_at": utc_now(),
        "project_root": str(root),
        "status": "ready" if editable_suffixes else "unsupported_project",
        "detected_languages": sorted(detected),
        "editable_languages": sorted(editable_languages),
        "editable_suffixes": editable_suffixes,
        "editable_top_level_roots": sorted(EDITABLE_TOP_LEVEL_ROOTS),
        "max_edit_files": MAX_EDIT_FILES,
        "max_edit_file_bytes": MAX_EDIT_FILE_BYTES,
        "max_edit_total_bytes": MAX_EDIT_TOTAL_BYTES,
        "dependency_manifest_editing_allowed": False,
        "candidate_only": True,
        "verification_required": True,
        "package_installation_allowed": False,
        "main_tree_editing_allowed": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }


def _is_sensitive_name(name: str) -> bool:
    lower = name.casefold()
    if lower in _SENSITIVE_EXACT_NAMES:
        return True
    return lower.endswith(_SENSITIVE_SUFFIXES)


def _copyable_relative(relative: PurePosixPath) -> bool:
    if not relative.parts:
        return False
    if any(part in IGNORED_COPY_DIRS for part in relative.parts[:-1]):
        return False
    if any(part.startswith(".") for part in relative.parts[:-1]):
        return False
    name = relative.name
    if _is_sensitive_name(name):
        return False
    if len(relative.parts) == 1 and name in COPY_MANIFESTS:
        return True
    suffix = relative.suffix.casefold()
    return (
        suffix in SUPPORTED_EDIT_SUFFIXES
        or suffix in VERIFICATION_SUPPORT_SUFFIXES
    )


def _iter_copyable_files(root: Path):
    root = Path(root).resolve()
    copied_count = 0
    copied_bytes = 0

    for current, dirs, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in IGNORED_COPY_DIRS
            and not name.startswith(".")
            and not (current_path / name).is_symlink()
        )

        for name in sorted(names):
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if not _copyable_relative(relative):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > MAX_COPY_FILE_BYTES:
                continue
            if copied_count + 1 > MAX_COPY_FILES:
                raise EngineeringCandidateEditError(
                    "candidate copy exceeds file-count limit"
                )
            if copied_bytes + size > MAX_COPY_TOTAL_BYTES:
                raise EngineeringCandidateEditError(
                    "candidate copy exceeds total-byte limit"
                )
            copied_count += 1
            copied_bytes += size
            yield path, relative, size


def _copy_digest(root: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    count = 0
    total = 0
    for source, relative, size in _iter_copyable_files(root):
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        try:
            payload = source.read_bytes()
        except OSError as exc:
            raise EngineeringCandidateEditError(
                "candidate source file became unreadable"
            ) from exc
        digest.update(payload)
        digest.update(b"\0")
        count += 1
        total += size
    return digest.hexdigest(), count, total


def _copy_public_engineering_tree(
    root: Path,
    destination: Path,
) -> tuple[list[str], int]:
    copied: list[str] = []
    total = 0
    for source, relative, size in _iter_copyable_files(root):
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        copied.append(relative.as_posix())
        total += size
    return sorted(copied), total


def prepare_engineering_candidate_workspace(
    root: Path,
    destination: Path,
    *,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a bounded verification candidate outside the main project."""
    root = Path(root).resolve()
    raw_destination = Path(destination)

    if not root.is_dir():
        raise EngineeringCandidateEditError("project root must exist")
    if raw_destination.exists() or raw_destination.is_symlink():
        raise EngineeringCandidateEditError(
            "candidate destination must not already exist"
        )

    destination = raw_destination.absolute()
    if destination == root:
        raise EngineeringCandidateEditError(
            "candidate destination cannot be the project root"
        )
    try:
        destination.resolve(strict=False).relative_to(root)
    except ValueError:
        pass
    else:
        raise EngineeringCandidateEditError(
            "engineering candidate must be outside the main project tree"
        )

    edit_policy = (
        dict(policy)
        if isinstance(policy, Mapping)
        else build_engineering_edit_policy(root)
    )
    if edit_policy.get("schema") != "sira.engineering_edit_policy.v1":
        raise EngineeringCandidateEditError("invalid engineering edit policy")
    if edit_policy.get("status") != "ready":
        raise EngineeringCandidateEditError(
            "project has no supported editable language"
        )
    if Path(str(edit_policy.get("project_root"))).resolve() != root:
        raise EngineeringCandidateEditError(
            "engineering edit policy belongs to a different project"
        )

    before_digest, source_count, source_bytes = _copy_digest(root)

    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    copied_files, copied_bytes = _copy_public_engineering_tree(
        root, destination
    )
    if not copied_files:
        shutil.rmtree(destination, ignore_errors=True)
        raise EngineeringCandidateEditError(
            "candidate copy contains no supported project files"
        )

    candidate_digest, candidate_count, candidate_bytes = _copy_digest(
        destination
    )
    if (
        candidate_digest != before_digest
        or candidate_count != source_count
        or candidate_bytes != source_bytes
    ):
        shutil.rmtree(destination, ignore_errors=True)
        raise EngineeringCandidateEditError(
            "candidate copy digest mismatch"
        )

    manifest = {
        "schema": "sira.engineering_candidate_manifest.v1",
        "policy_version": ENGINEERING_EDIT_POLICY_VERSION,
        "created_at": utc_now(),
        "source_root": str(root),
        "candidate_root": str(destination),
        "source_copy_sha256": before_digest,
        "candidate_copy_sha256": candidate_digest,
        "copied_file_count": len(copied_files),
        "copied_bytes": copied_bytes,
        "edit_policy": edit_policy,
        "sensitive_artifact_patterns_excluded": True,
        "vendor_dependency_trees_excluded": True,
        "symlinks_copied": False,
        "main_tree_modified": False,
        "package_installation_performed": False,
        "promotion_allowed": False,
        "authority_granted": False,
    }

    manifest_path = (
        destination.parent
        / f"{destination.name}.engineering_candidate_manifest.json"
    )
    write_json(manifest_path, manifest)
    try:
        os.chmod(manifest_path, 0o600)
    except OSError:
        pass
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _validate_edit_relative_path(
    value: str,
    policy: Mapping[str, Any],
) -> tuple[str, str]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 320
        or "\\" in value
    ):
        raise EngineeringCandidateEditError(
            "candidate edit path must be a bounded POSIX relative path"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise EngineeringCandidateEditError(
            "candidate edit path traversal is forbidden"
        )

    normalized = path.as_posix()
    if normalized in PROTECTED_PATHS:
        raise EngineeringCandidateEditError(
            "candidate edit targets a protected SIRA path"
        )
    if path.name in DENIED_EDIT_NAMES:
        raise EngineeringCandidateEditError(
            "dependency/build configuration editing is disabled"
        )
    if any(
        part in IGNORED_COPY_DIRS or part.startswith(".")
        for part in path.parts[:-1]
    ):
        raise EngineeringCandidateEditError(
            "candidate edit targets an excluded directory"
        )
    if _is_sensitive_name(path.name):
        raise EngineeringCandidateEditError(
            "candidate edit targets a sensitive artifact"
        )

    suffix = path.suffix.casefold()
    allowed_suffixes = policy.get("editable_suffixes")
    allowed_suffixes = (
        set(allowed_suffixes)
        if isinstance(allowed_suffixes, list)
        else set()
    )
    if suffix not in SUPPORTED_EDIT_SUFFIXES or suffix not in allowed_suffixes:
        raise EngineeringCandidateEditError(
            "candidate edit language is not enabled for this project"
        )

    if len(path.parts) > 1:
        first = path.parts[0]
        if first not in EDITABLE_TOP_LEVEL_ROOTS:
            raise EngineeringCandidateEditError(
                "candidate edit path is outside supported source roots"
            )

    language = language_for_path(normalized)
    if language is None:
        raise EngineeringCandidateEditError(
            "candidate edit language is unsupported"
        )
    return normalized, language


class ValidatedEngineeringCandidateEditor:
    """Apply bounded language-aware edits inside a prepared candidate only."""

    def __init__(self, policy: Mapping[str, Any]):
        if (
            not isinstance(policy, Mapping)
            or policy.get("schema") != "sira.engineering_edit_policy.v1"
            or policy.get("status") != "ready"
            or policy.get("candidate_only") is not True
            or policy.get("promotion_authorized") is not False
        ):
            raise EngineeringCandidateEditError(
                "invalid engineering edit policy"
            )
        self.policy = dict(policy)

    def apply_text_edits(
        self,
        candidate_root: Path,
        edits: Mapping[str, str],
    ) -> dict[str, Any]:
        raw_root = Path(candidate_root)
        if raw_root.is_symlink():
            raise EngineeringCandidateEditError(
                "candidate root is missing or unsafe"
            )
        candidate_root = raw_root.resolve()
        if not candidate_root.is_dir():
            raise EngineeringCandidateEditError(
                "candidate root is missing or unsafe"
            )
        if not isinstance(edits, Mapping) or not edits:
            raise EngineeringCandidateEditError(
                "at least one engineering edit is required"
            )
        if len(edits) > MAX_EDIT_FILES:
            raise EngineeringCandidateEditError(
                "too many files in one engineering edit batch"
            )

        validated: list[tuple[str, str, Path, bytes]] = []
        total_bytes = 0

        for raw_path, content in edits.items():
            normalized, language = _validate_edit_relative_path(
                raw_path, self.policy
            )
            if not isinstance(content, str) or "\x00" in content:
                raise EngineeringCandidateEditError(
                    "engineering edits must be UTF-8 text without NUL bytes"
                )
            payload = content.encode("utf-8")
            if len(payload) > MAX_EDIT_FILE_BYTES:
                raise EngineeringCandidateEditError(
                    "engineering edit exceeds per-file size limit"
                )
            total_bytes += len(payload)
            if total_bytes > MAX_EDIT_TOTAL_BYTES:
                raise EngineeringCandidateEditError(
                    "engineering edit batch exceeds total size limit"
                )

            relative = PurePosixPath(normalized)
            target = candidate_root.joinpath(*relative.parts)

            current = candidate_root
            for part in relative.parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise EngineeringCandidateEditError(
                        "engineering edit traverses a symlink"
                    )
            if target.is_symlink():
                raise EngineeringCandidateEditError(
                    "engineering edit target is a symlink"
                )

            parent = target.parent.resolve()
            try:
                parent.relative_to(candidate_root)
            except ValueError:
                raise EngineeringCandidateEditError(
                    "engineering edit escapes candidate root"
                ) from None

            validated.append(
                (normalized, language, target, payload)
            )

        changed: list[str] = []
        hashes: dict[str, str] = {}
        languages: dict[str, str] = {}

        for normalized, language, target, payload in validated:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".sira-edit-tmp")
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, target)

            changed.append(normalized)
            hashes[normalized] = hashlib.sha256(payload).hexdigest()
            languages[normalized] = language

        return {
            "schema": "sira.engineering_edit_result.v1",
            "policy_version": ENGINEERING_EDIT_POLICY_VERSION,
            "files_changed": sorted(changed),
            "file_sha256": {
                key: hashes[key] for key in sorted(hashes)
            },
            "file_languages": {
                key: languages[key] for key in sorted(languages)
            },
            "bytes_written": total_bytes,
            "candidate_only": True,
            "verification_required": True,
            "main_tree_modified": False,
            "package_installation_performed": False,
            "authority_granted": False,
            "promotion_performed": False,
            "promotion_authorized": False,
        }


def prepare_engineering_edit_candidate(
    root: Path,
    workspace: Path,
    edits: Mapping[str, str],
) -> dict[str, Any]:
    """Prepare, edit, and plan verification without executing or promoting."""
    root = Path(root).resolve()
    workspace = Path(workspace).absolute()

    if workspace.exists() or workspace.is_symlink():
        raise EngineeringCandidateEditError(
            "engineering edit workspace must not already exist"
        )
    try:
        workspace.resolve(strict=False).relative_to(root)
    except ValueError:
        pass
    else:
        raise EngineeringCandidateEditError(
            "engineering edit workspace must be outside the main project"
        )

    workspace.mkdir(parents=True, mode=0o700, exist_ok=False)
    candidate_root = workspace / "candidate"

    policy = build_engineering_edit_policy(root)
    manifest = prepare_engineering_candidate_workspace(
        root,
        candidate_root,
        policy=policy,
    )
    edit_report = ValidatedEngineeringCandidateEditor(
        policy
    ).apply_text_edits(candidate_root, edits)

    candidate_profile = inspect_project(candidate_root)
    verification_plans: dict[str, dict[str, Any]] = {}
    for relative in edit_report["files_changed"]:
        verification_plans[relative] = plan_verification(
            candidate_root,
            profile=candidate_profile,
            target_path=relative,
        )

    result = {
        "schema": "sira.engineering_edit_candidate.v1",
        "policy_version": ENGINEERING_EDIT_POLICY_VERSION,
        "created_at": utc_now(),
        "source_root": str(root),
        "workspace": str(workspace),
        "candidate_root": str(candidate_root),
        "status": "candidate_prepared",
        "policy": policy,
        "candidate_manifest": manifest,
        "edit_report": edit_report,
        "verification_plans": verification_plans,
        "verification_executed": False,
        "verification_required": True,
        "main_tree_modified": False,
        "package_installation_performed": False,
        "authority_granted": False,
        "promotion_performed": False,
        "promotion_authorized": False,
    }
    write_json(
        workspace / "engineering_edit_candidate.json",
        result,
    )
    return result
