"""Protected rollback primitives for transactional self-promotion."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import hashlib
import os
import shutil
from typing import Iterable


class RollbackError(RuntimeError):
    """Raised when a protected rollback cannot restore the pre-promotion tree."""


@dataclass(frozen=True)
class BackupEntry:
    path: str
    existed: bool
    sha256: str | None


def _safe_target(root: Path, relative: str) -> Path:
    root = Path(root).resolve()
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise RollbackError("unsafe rollback path")
    current = root
    for part in path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise RollbackError("rollback path traverses a symlink")
    target = root.joinpath(*path.parts)
    if target.is_symlink():
        raise RollbackError("rollback target is a symlink")
    try:
        target.parent.resolve().relative_to(root)
    except ValueError:
        raise RollbackError("rollback path escapes project root") from None
    return target


def create_backup(main_root: Path, backup_root: Path, changed_files: Iterable[str]) -> list[BackupEntry]:
    """Snapshot exactly the files a promotion is about to replace."""
    main_root = Path(main_root).resolve()
    backup_root = Path(backup_root).resolve()
    backup_root.mkdir(parents=True, mode=0o700, exist_ok=False)
    entries: list[BackupEntry] = []
    for relative in sorted(set(changed_files)):
        source = _safe_target(main_root, relative)
        if source.exists() and not source.is_file():
            raise RollbackError("promotion target must be a regular file or absent")
        if source.is_file():
            payload = source.read_bytes()
            destination = backup_root.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
            entries.append(BackupEntry(relative, True, hashlib.sha256(payload).hexdigest()))
        else:
            entries.append(BackupEntry(relative, False, None))
    return entries


def restore_backup(main_root: Path, backup_root: Path, entries: Iterable[BackupEntry]) -> None:
    """Restore modified files and remove files that did not exist before promotion."""
    main_root = Path(main_root).resolve()
    backup_root = Path(backup_root).resolve()
    failures: list[str] = []
    for entry in entries:
        try:
            target = _safe_target(main_root, entry.path)
            if entry.existed:
                source = backup_root.joinpath(*PurePosixPath(entry.path).parts)
                if not source.is_file() or source.is_symlink():
                    raise RollbackError("rollback backup is missing or unsafe")
                payload = source.read_bytes()
                if hashlib.sha256(payload).hexdigest() != entry.sha256:
                    raise RollbackError("rollback backup checksum mismatch")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".sira-rollback.tmp")
                if temporary.exists() or temporary.is_symlink():
                    temporary.unlink()
                shutil.copy2(source, temporary, follow_symlinks=False)
                os.replace(temporary, target)
            elif target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_file():
                    raise RollbackError("new promotion target became unsafe")
                target.unlink()
        except (OSError, RollbackError) as exc:
            failures.append(f"{entry.path}: {type(exc).__name__}")
    if failures:
        raise RollbackError("rollback incomplete: " + "; ".join(failures))
