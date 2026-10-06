"""Hardened candidate-workspace and autonomous text-edit boundary.

v1.0B-1 exposes a writer contract for isolated candidates only. It never
promotes a candidate into SIRA's main tree.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
from typing import Mapping, Protocol

from .models import utc_now
from .storage import write_json

SELF_MODIFICATION_POLICY_VERSION = 1
MAX_EDIT_FILES = 16
MAX_EDIT_FILE_BYTES = 256 * 1024
MAX_EDIT_TOTAL_BYTES = 1024 * 1024

# Files needed to execute/evaluate an isolated candidate. This is an explicit
# copy allowlist, so runtime data, credentials, Git metadata, cache and memory
# are never copied merely because they happen to exist under the project root.
PUBLIC_COPY_ENTRIES = (
    "sira.py",
    "src",
    "tests",
    "benchmarks",
    "docs",
    "desktop",
    "tools",
    "README.md",
    ".env.example",
    ".gitignore",
)

MODIFIABLE_ROOTS = (
    "src/sira",
    "tests",
    "benchmarks",
    "docs",
)

# This is intentionally broader than files that exist today. Future gate files
# become protected automatically when introduced under these canonical names.
PROTECTED_PATHS = frozenset({
    "src/sira/runtime.py",
    "src/sira/cli.py",
    "src/sira/self_modification.py",
    "src/sira/evaluator1.py",
    "src/sira/engineering_authorization.py",
    "src/sira/engineering_promotion.py",
    "src/sira/engineering_runtime.py",
    "src/sira/engineering_canary.py",
    "src/sira/runtime_soak.py",
    "src/sira/runtime_release.py",
    "src/sira/desktop_app.py",
    "src/sira/desktop_chat.py",
    "src/sira/desktop_research.py",
    "src/sira/providers/gemini_chat.py",
    "src/sira/promotion.py",
    "src/sira/rollback.py",
    "src/sira/autonomous_promotion.py",
    "src/sira/owner_identity.py",
    "src/sira/audit_integrity.py",
    "src/sira/safety_boundary.py",
})

_EXCLUDED_NAMES = frozenset({"__pycache__", ".cache"})
_EXCLUDED_SUFFIXES = (".pyc", ".pyo")


class CandidateEditError(ValueError):
    """Raised when an autonomous candidate edit violates the boundary."""


class CandidateCodeWriter(Protocol):
    """Contract for a future research/model-backed autonomous code writer.

    Writers return complete UTF-8 text replacements keyed by candidate-relative
    POSIX path. Only :class:`ValidatedCandidateEditor` may apply them.
    """

    def propose_text_edits(self, context: Mapping[str, object]) -> Mapping[str, str]: ...


def _safe_public_files(root: Path):
    root = Path(root).resolve()
    for entry in PUBLIC_COPY_ENTRIES:
        source = root / entry
        if not source.exists() or source.is_symlink():
            continue
        if source.is_file():
            yield source, Path(entry)
            continue
        for current, dirs, files in os.walk(source, followlinks=False):
            current_path = Path(current)
            dirs[:] = [
                name for name in dirs
                if name not in _EXCLUDED_NAMES and not (current_path / name).is_symlink()
            ]
            for name in files:
                src = current_path / name
                if src.is_symlink() or name.endswith(_EXCLUDED_SUFFIXES):
                    continue
                yield src, src.relative_to(root)


def _public_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for source, relative in sorted(_safe_public_files(root), key=lambda item: item[1].as_posix()):
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _copy_public_tree(root: Path, destination: Path) -> list[str]:
    copied: list[str] = []
    for source, relative in _safe_public_files(root):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        copied.append(relative.as_posix())
    return sorted(copied)


def prepare_candidate_workspace(root: Path, destination: Path) -> dict[str, object]:
    """Create an isolated public candidate copy and a policy manifest.

    The destination must not already exist and must not be inside a source tree
    that is itself copied, preventing recursive/self-overwriting candidates.
    """
    root = Path(root).resolve()
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise CandidateEditError("candidate destination must not already exist")
    if destination == root:
        raise CandidateEditError("candidate destination cannot be the project root")
    try:
        rel_destination = destination.relative_to(root)
    except ValueError:
        rel_destination = None
    if rel_destination is not None:
        rel_posix = rel_destination.as_posix()
        if any(rel_posix == base or rel_posix.startswith(base + "/")
               for base in ("src", "tests", "benchmarks", "docs")):
            raise CandidateEditError("candidate destination cannot be inside a copied source tree")

    source_digest = _public_tree_digest(root)
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    copied_files = _copy_public_tree(root, destination)
    candidate_digest = _public_tree_digest(destination)
    manifest = {
        "schema_version": 1,
        "kind": "candidate_workspace_manifest",
        "policy_version": SELF_MODIFICATION_POLICY_VERSION,
        "created_at": utc_now(),
        "candidate_root": str(destination),
        "source_public_sha256": source_digest,
        "candidate_public_sha256": candidate_digest,
        "copied_file_count": len(copied_files),
        "modifiable_roots": list(MODIFIABLE_ROOTS),
        "protected_paths": sorted(PROTECTED_PATHS),
        "promotion_allowed": False,
        "credentials_copied": False,
    }
    manifest_path = destination.parent / "candidate_manifest.json"
    write_json(manifest_path, manifest)
    try:
        os.chmod(manifest_path, 0o600)
    except OSError:
        pass
    return manifest


def _validate_relative_edit_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CandidateEditError("candidate edit path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise CandidateEditError("candidate edit path traversal or absolute paths are forbidden")
    normalized = path.as_posix()
    if normalized in PROTECTED_PATHS:
        raise CandidateEditError("candidate edit targets a protected shell path")
    if not any(normalized.startswith(root + "/") for root in MODIFIABLE_ROOTS):
        raise CandidateEditError("candidate edit path is outside modifiable roots")
    return normalized


class ValidatedCandidateEditor:
    """Apply bounded full-text replacements inside an isolated candidate."""

    def apply_text_edits(self, candidate_root: Path, edits: Mapping[str, str]) -> dict[str, object]:
        raw_candidate_root = Path(candidate_root)
        if raw_candidate_root.is_symlink():
            raise CandidateEditError("candidate root is missing or unsafe")
        candidate_root = raw_candidate_root.resolve()
        if not candidate_root.is_dir():
            raise CandidateEditError("candidate root is missing or unsafe")
        if not isinstance(edits, Mapping) or not edits:
            raise CandidateEditError("at least one candidate text edit is required")
        if len(edits) > MAX_EDIT_FILES:
            raise CandidateEditError("too many files in one candidate edit batch")

        validated: list[tuple[str, Path, bytes]] = []
        total_bytes = 0
        for raw_path, content in edits.items():
            normalized = _validate_relative_edit_path(raw_path)
            if not isinstance(content, str) or "\x00" in content:
                raise CandidateEditError("candidate edits must be UTF-8 text without NUL bytes")
            payload = content.encode("utf-8")
            if len(payload) > MAX_EDIT_FILE_BYTES:
                raise CandidateEditError("candidate edit exceeds per-file size limit")
            total_bytes += len(payload)
            if total_bytes > MAX_EDIT_TOTAL_BYTES:
                raise CandidateEditError("candidate edit batch exceeds total size limit")

            target = candidate_root.joinpath(*PurePosixPath(normalized).parts)
            # Resolve only the parent: a new file is valid, but no existing
            # symlink at any parent or at the target may redirect the write.
            current = candidate_root
            for part in PurePosixPath(normalized).parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise CandidateEditError("candidate edit traverses a symlink")
            if target.is_symlink():
                raise CandidateEditError("candidate edit target is a symlink")
            parent_resolved = target.parent.resolve()
            try:
                parent_resolved.relative_to(candidate_root)
            except ValueError:
                raise CandidateEditError("candidate edit escapes candidate root") from None
            validated.append((normalized, target, payload))

        changed: list[str] = []
        hashes: dict[str, str] = {}
        for normalized, target, payload in validated:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".sira-tmp")
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            changed.append(normalized)
            hashes[normalized] = hashlib.sha256(payload).hexdigest()

        return {
            "policy_version": SELF_MODIFICATION_POLICY_VERSION,
            "files_changed": sorted(changed),
            "file_sha256": {key: hashes[key] for key in sorted(hashes)},
            "bytes_written": total_bytes,
            "promotion_performed": False,
        }
