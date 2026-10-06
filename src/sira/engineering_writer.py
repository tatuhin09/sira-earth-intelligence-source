"""Bounded multi-language engineering writer + isolated verification orchestration.

This path is parallel to SIRA's existing Python self-modification pipeline.
It can generate structured source edits for a supported engineering project,
apply them only inside a v1.8D candidate, then execute the v1.8B verifier and
surface v1.8C structured diagnostics. It never promotes or mutates the main tree.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping, Protocol

from .code_writer import CodeModelBatch
from .engineering_diagnostics import diagnostic_advisory_context
from .engineering_scoped_edit import (
    EngineeringScopedEditError,
    ScopedPythonSymbolEdit,
    build_python_symbol_scope,
    compact_engineering_model_context,
    materialize_engineering_edits,
)
from .engineering_editing import (
    COPY_MANIFESTS,
    EDITABLE_TOP_LEVEL_ROOTS,
    IGNORED_COPY_DIRS,
    MAX_EDIT_FILE_BYTES,
    SUPPORTED_EDIT_SUFFIXES,
    EngineeringCandidateEditError,
    build_engineering_edit_policy,
    prepare_engineering_edit_candidate,
)
from .engineering_verification import execute_verification_plan
from .models import utc_now
from .storage import write_json

ENGINEERING_WRITER_POLICY_VERSION = 1

PRIMARY_CONTEXT_FILE_LIMIT = 8
PRIMARY_CONTEXT_TOTAL_BYTES = 96 * 1024
EXPANDED_CONTEXT_FILE_LIMIT = 12
EXPANDED_CONTEXT_TOTAL_BYTES = 192 * 1024
MAX_CONTEXT_FILE_BYTES = 96 * 1024
MAX_CONTEXT_INDEX_FILES = 80
MAX_CONTEXT_REQUESTS = 4
MAX_GENERATED_EDITS = 8
MAX_GENERATED_PATCH_BYTES = 512 * 1024
MAX_REASON_CHARS = 1000
MAX_SUMMARY_CHARS = 2000
MAX_TASK_JSON_BYTES = 32 * 1024
MAX_INSTRUCTION_CHARS = 6000
MAX_DIAGNOSTIC_ROWS = 20
MAX_VERIFICATION_COMMANDS = 8

_WORD = re.compile(r"[A-Za-z0-9_]{3,}")

ENGINEERING_PATCH_SCHEMA = {
    "type": "object",
    "required": ["summary", "edits"],
    "properties": {
        "summary": {"type": "string"},
        "edits": {
            "type": "array",
            "maxItems": MAX_GENERATED_EDITS,
            "items": {
                "type": "object",
                "required": ["path", "content", "reason"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "reason": {"type": "string"},
                    "symbol": {"type": "string"},
                },
            },
        },
        "needs_more_context": {"type": "boolean"},
        "context_requests": {
            "type": "array",
            "maxItems": MAX_CONTEXT_REQUESTS,
            "items": {"type": "string"},
        },
    },
}


def _patch_schema_for_context(
    context: Mapping[str, object],
) -> dict[str, object]:
    schema = deepcopy(ENGINEERING_PATCH_SCHEMA)
    scope = context.get("scoped_edit")
    if (
        isinstance(scope, Mapping)
        and scope.get("mode") == "python_symbol_block"
    ):
        required = schema["properties"]["edits"]["items"]["required"]
        required.append("symbol")
    return schema


def _scoped_target_covers_complete_file(
    context: Mapping[str, object],
) -> bool:
    scope = context.get("scoped_edit")
    rows = context.get("files")
    if (
        not isinstance(scope, Mapping)
        or scope.get("mode") != "python_symbol_block"
        or not isinstance(rows, list)
    ):
        return False
    relative = scope.get("path")
    start = scope.get("start_line")
    end = scope.get("end_line")
    if (
        not isinstance(relative, str)
        or type(start) is not int
        or type(end) is not int
        or not 1 <= start <= end
    ):
        return False
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and row.get("path") == relative
        and isinstance(row.get("content"), str)
    ]
    if len(matches) != 1:
        return False
    lines = matches[0]["content"].splitlines(keepends=True)
    if end > len(lines):
        return False
    outside = "".join(lines[: start - 1] + lines[end:])
    return not outside.strip()


class EngineeringPatchModel(Protocol):
    name: str
    model_id: str

    def generate_patch(
        self,
        payload: dict,
        schema: dict,
    ) -> CodeModelBatch: ...


class EngineeringWriterOutputError(ValueError):
    """Raised when engineering model output violates the structured contract."""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = _stable_output_error_code(message)


def _stable_output_error_code(message: object) -> str:
    """Return a bounded code without persisting provider output or source."""
    if (
        not isinstance(message, str)
        or not message
        or len(message) > 120
        or re.fullmatch(r"[A-Za-z0-9_ -]+", message) is None
    ):
        return "invalid_engineering_model_output"
    code = re.sub(r"[^a-z0-9]+", "_", message.casefold()).strip("_")
    return code[:120] or "invalid_engineering_model_output"


def _is_sensitive_name(name: str) -> bool:
    lower = name.casefold()
    if lower in {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        ".env.test",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
        "service-account.json",
    }:
        return True
    return lower.endswith(
        (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
    )


def _bounded_task(task: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(task, Mapping):
        raise ValueError("engineering task must be a mapping")

    instruction = task.get("instruction")
    if (
        not isinstance(instruction, str)
        or not instruction.strip()
        or len(instruction) > MAX_INSTRUCTION_CHARS
    ):
        raise ValueError("engineering task instruction is invalid")

    raw_targets = task.get("target_paths", [])
    if raw_targets is None:
        raw_targets = []
    if not isinstance(raw_targets, list) or len(raw_targets) > 8:
        raise ValueError("engineering target_paths must be a bounded list")
    target_paths: list[str] = []
    for value in raw_targets:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 320
            or "\\" in value
        ):
            raise ValueError("engineering target path is invalid")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("engineering target path traversal is forbidden")
        normalized = path.as_posix()
        if normalized not in target_paths:
            target_paths.append(normalized)

    raw_diagnostics = task.get("diagnostics", [])
    if raw_diagnostics is None:
        raw_diagnostics = []
    if not isinstance(raw_diagnostics, list):
        raise ValueError("engineering diagnostics must be a list")
    diagnostics: list[dict[str, object]] = []
    for row in raw_diagnostics[:MAX_DIAGNOSTIC_ROWS]:
        if not isinstance(row, Mapping):
            continue
        diagnostics.append({
            key: row.get(key)
            for key in (
                "fingerprint",
                "tool",
                "command_id",
                "language",
                "severity",
                "code",
                "path",
                "line",
                "column",
                "message",
            )
        })

    target_symbol = None
    raw_target = task.get("target")
    if isinstance(raw_target, Mapping):
        raw_symbol = raw_target.get("symbol")
        if raw_symbol is not None:
            if (
                not isinstance(raw_symbol, str)
                or not raw_symbol.isidentifier()
                or len(raw_symbol) > 240
            ):
                raise ValueError("engineering target symbol is invalid")
            target_symbol = raw_symbol

    result: dict[str, object] = {
        "task_id": task.get("task_id"),
        "instruction": instruction.strip(),
        "target_paths": target_paths,
        "target_symbol": target_symbol,
        "language_hint": task.get("language_hint"),
        "success_criteria": task.get("success_criteria"),
        "diagnostics": diagnostics,
    }
    try:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValueError(
            "engineering task contains non-JSON data"
        ) from None
    if len(encoded) > MAX_TASK_JSON_BYTES:
        raise ValueError(
            "engineering task exceeds local context limit"
        )
    return result


def _safe_context_relative(
    value: str,
    policy: Mapping[str, Any],
) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 320
        or "\\" in value
    ):
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return False
    if any(
        part in IGNORED_COPY_DIRS or part.startswith(".")
        for part in path.parts[:-1]
    ):
        return False
    if _is_sensitive_name(path.name):
        return False

    if len(path.parts) == 1 and path.name in COPY_MANIFESTS:
        return True

    suffixes = policy.get("editable_suffixes")
    allowed = set(suffixes) if isinstance(suffixes, list) else set()
    return (
        path.suffix.casefold() in SUPPORTED_EDIT_SUFFIXES
        and path.suffix.casefold() in allowed
    )


def _iter_context_files(
    root: Path,
    policy: Mapping[str, Any],
):
    root = Path(root).resolve()
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
            relative = path.relative_to(root).as_posix()
            if not _safe_context_relative(relative, policy):
                continue
            try:
                payload = path.read_bytes()
            except OSError:
                continue
            if (
                len(payload) > MAX_CONTEXT_FILE_BYTES
                or b"\x00" in payload
            ):
                continue
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                continue
            yield (
                relative,
                text,
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            )


def _search_tokens(task: Mapping[str, object]) -> set[str]:
    bounded = _bounded_task(task)
    text = json.dumps(
        bounded,
        ensure_ascii=False,
    ).casefold()
    return {
        match.group(0)
        for match in _WORD.finditer(text)
        if len(match.group(0)) >= 4
    }


def _file_score(
    relative: str,
    content: str,
    tokens: set[str],
    targets: set[str],
) -> int:
    score = 0
    if relative in targets:
        score += 5000
    lower_path = relative.casefold()
    lower_text = content.casefold()
    for token in tokens:
        if token in lower_path:
            score += 30
        count = lower_text.count(token)
        if count:
            score += min(count, 4) * 2
    if relative.startswith(
        (
            "src/",
            "app/",
            "lib/",
            "server/",
            "client/",
            "web/",
            "api/",
            "cmd/",
            "internal/",
            "pkg/",
        )
    ):
        score += 5
    elif relative.startswith(("test/", "tests/")):
        score += 4
    elif PurePosixPath(relative).name in COPY_MANIFESTS:
        score += 2
    return score


def _load_context_file(
    root: Path,
    relative: str,
    policy: Mapping[str, Any],
) -> tuple[str, int, str] | None:
    if not _safe_context_relative(relative, policy):
        return None
    path = root / relative
    try:
        if path.is_symlink() or not path.is_file():
            return None
        payload = path.read_bytes()
    except OSError:
        return None
    if (
        len(payload) > MAX_CONTEXT_FILE_BYTES
        or b"\x00" in payload
    ):
        return None
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return (
        content,
        len(payload),
        hashlib.sha256(payload).hexdigest(),
    )


def build_engineering_writer_context(
    root: Path,
    task: Mapping[str, object],
    *,
    expanded_paths: tuple[str, ...] | list[str] | None = None,
) -> dict[str, object]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("engineering project root must exist")

    policy = build_engineering_edit_policy(root)
    if policy.get("status") != "ready":
        raise ValueError(
            "engineering project has no supported editable language"
        )

    bounded_task = _bounded_task(task)
    targets = set(
        str(value)
        for value in bounded_task.get("target_paths", [])
        if isinstance(value, str)
    )
    tokens = _search_tokens(task)

    candidates: list[
        tuple[int, str, str, int, str]
    ] = []
    index_rows: list[dict[str, object]] = []

    for relative, content, size, digest in _iter_context_files(
        root,
        policy,
    ):
        score = _file_score(
            relative,
            content,
            tokens,
            targets,
        )
        index_rows.append({
            "path": relative,
            "bytes": size,
            "score": score,
            "sha256": digest,
            "editable": (
                PurePosixPath(relative).suffix.casefold()
                in set(policy["editable_suffixes"])
            ),
        })
        if score > 0:
            candidates.append(
                (score, relative, content, size, digest)
            )

    index_rows.sort(
        key=lambda row: (
            -int(row["score"]),
            str(row["path"]),
        )
    )
    available_index = index_rows[:MAX_CONTEXT_INDEX_FILES]

    requested: list[str] = []
    if expanded_paths is not None:
        for relative in expanded_paths:
            if not _safe_context_relative(relative, policy):
                raise EngineeringWriterOutputError(
                    "invalid engineering context request"
                )
            if relative not in requested:
                requested.append(relative)
        if (
            not requested
            or len(requested) > MAX_CONTEXT_REQUESTS
        ):
            raise EngineeringWriterOutputError(
                "engineering context expansion is invalid"
            )
        tier = "expanded"
        file_limit = EXPANDED_CONTEXT_FILE_LIMIT
        byte_limit = EXPANDED_CONTEXT_TOTAL_BYTES
    else:
        tier = "primary"
        file_limit = PRIMARY_CONTEXT_FILE_LIMIT
        byte_limit = PRIMARY_CONTEXT_TOTAL_BYTES

    selected: list[dict[str, object]] = []
    total_bytes = 0

    if tier == "expanded":
        ordered = list(requested)
        ordered.extend(
            row[1]
            for row in sorted(
                candidates,
                key=lambda item: (-item[0], item[1]),
            )
        )
        seen: set[str] = set()
        for relative in ordered:
            if relative in seen:
                continue
            seen.add(relative)
            loaded = _load_context_file(
                root,
                relative,
                policy,
            )
            if loaded is None:
                if relative in requested:
                    raise EngineeringWriterOutputError(
                        "requested engineering context is unavailable"
                    )
                continue
            content, size, digest = loaded
            if (
                len(selected) >= file_limit
                or total_bytes + size > byte_limit
            ):
                if relative in requested:
                    raise EngineeringWriterOutputError(
                        "requested engineering context exceeds budget"
                    )
                continue
            selected.append({
                "path": relative,
                "sha256": digest,
                "content": content,
                "editable": (
                    PurePosixPath(relative).suffix.casefold()
                    in set(policy["editable_suffixes"])
                ),
            })
            total_bytes += size
    else:
        ordered = sorted(
            candidates,
            key=lambda item: (-item[0], item[1]),
        )
        for _, relative, content, size, digest in ordered:
            if (
                len(selected) >= file_limit
                or total_bytes + size > byte_limit
            ):
                continue
            selected.append({
                "path": relative,
                "sha256": digest,
                "content": content,
                "editable": (
                    PurePosixPath(relative).suffix.casefold()
                    in set(policy["editable_suffixes"])
                ),
            })
            total_bytes += size

    scoped_edit = None
    if len(targets) == 1:
        scoped_target = next(iter(targets))
        for row in selected:
            if row.get("path") == scoped_target:
                scoped_edit = build_python_symbol_scope(
                    row,
                    bounded_task,
                )
                break

    safe_policy = {
        "editable_languages": policy["editable_languages"],
        "editable_suffixes": policy["editable_suffixes"],
        "editable_top_level_roots": policy[
            "editable_top_level_roots"
        ],
        "dependency_manifest_editing_allowed": False,
        "candidate_only": True,
        "verification_required": True,
        "package_installation_allowed": False,
    }

    base = {
        "schema": "sira.engineering_writer_context.v1",
        "policy_version": ENGINEERING_WRITER_POLICY_VERSION,
        "context_tier": tier,
        "task": bounded_task,
        "files": selected,
        "context_file_count": len(selected),
        "context_bytes": total_bytes,
        "context_budget_bytes": byte_limit,
        "requested_paths": requested,
        "available_context_index": available_index,
        "scoped_edit": scoped_edit,
        "editable_path_policy": safe_policy,
        "read_only_manifests": sorted(COPY_MANIFESTS),
        "output_contract": (
            "when scoped_edit.mode is python_symbol_block, return the exact "
            "path and symbol with only the replacement target definition; do "
            "not add sibling helpers or return the whole file; "
            "otherwise use complete UTF-8 source-file replacements; dependency/"
            "build manifests are context-only and never editable; request one "
            "bounded context expansion before changing an existing file whose "
            "complete content is absent"
        ),
    }
    canonical = json.dumps(
        base,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **base,
        "context_sha256": hashlib.sha256(
            canonical
        ).hexdigest(),
    }


def _validate_model_patch(
    value: object,
    policy: Mapping[str, Any],
    scoped_edit: Mapping[str, object] | None = None,
    *,
    allow_implicit_whole_file_symbol: bool = False,
) -> tuple[
    str,
    dict[str, str],
    list[dict[str, str]],
    bool,
    list[str],
]:
    if not isinstance(value, dict):
        raise EngineeringWriterOutputError(
            "engineering model output must be an object"
        )
    allowed_keys = {
        "summary",
        "edits",
        "needs_more_context",
        "context_requests",
    }
    if (
        not {"summary", "edits"}.issubset(value)
        or not set(value).issubset(allowed_keys)
    ):
        raise EngineeringWriterOutputError(
            "invalid engineering model output keys"
        )

    summary = value.get("summary")
    edits = value.get("edits")
    needs_more = value.get(
        "needs_more_context",
        False,
    )
    requests = value.get("context_requests", [])

    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary) > MAX_SUMMARY_CHARS
    ):
        raise EngineeringWriterOutputError(
            "invalid engineering model summary"
        )
    if (
        not isinstance(edits, list)
        or len(edits) > MAX_GENERATED_EDITS
    ):
        raise EngineeringWriterOutputError(
            "invalid engineering edit list"
        )
    if type(needs_more) is not bool:
        raise EngineeringWriterOutputError(
            "needs_more_context must be boolean"
        )
    if (
        not isinstance(requests, list)
        or len(requests) > MAX_CONTEXT_REQUESTS
    ):
        raise EngineeringWriterOutputError(
            "invalid engineering context request list"
        )

    clean_requests: list[str] = []
    for relative in requests:
        if (
            not isinstance(relative, str)
            or not _safe_context_relative(
                relative,
                policy,
            )
        ):
            raise EngineeringWriterOutputError(
                "invalid engineering context request"
            )
        if relative not in clean_requests:
            clean_requests.append(relative)

    if needs_more and edits:
        raise EngineeringWriterOutputError(
            "context expansion cannot include edits"
        )
    if needs_more and not clean_requests:
        raise EngineeringWriterOutputError(
            "context expansion requires exact paths"
        )

    authorized_scope = (
        scoped_edit
        if (
            isinstance(scoped_edit, Mapping)
            and scoped_edit.get("mode") == "python_symbol_block"
        )
        else None
    )
    if authorized_scope is not None and edits and len(edits) != 1:
        raise EngineeringWriterOutputError(
            "scoped engineering output must use authorized symbol"
        )

    mapped: dict[str, object] = {}
    metadata: list[dict[str, str]] = []
    total_bytes = 0

    for item in edits:
        if not isinstance(item, dict):
            raise EngineeringWriterOutputError(
                "invalid engineering edit item"
            )
        item_keys = set(item)
        if item_keys not in (
            {"path", "content", "reason"},
            {"path", "content", "reason", "symbol"},
        ):
            raise EngineeringWriterOutputError(
                "invalid engineering edit item"
            )
        path = item.get("path")
        content = item.get("content")
        reason = item.get("reason")
        symbol = item.get("symbol")

        if authorized_scope is not None:
            if (
                symbol is None
                and allow_implicit_whole_file_symbol
                and path == authorized_scope.get("path")
            ):
                symbol = authorized_scope.get("symbol")
            if (
                path != authorized_scope.get("path")
                or symbol != authorized_scope.get("symbol")
            ):
                raise EngineeringWriterOutputError(
                    "scoped engineering output must use authorized symbol"
                )

        if (
            not isinstance(path, str)
            or not path
            or len(path) > 320
            or "\\" in path
            or not isinstance(content, str)
            or "\x00" in content
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > MAX_REASON_CHARS
        ):
            raise EngineeringWriterOutputError(
                "invalid engineering edit fields"
            )
        if symbol is not None and (
            not isinstance(symbol, str)
            or not symbol.isidentifier()
            or len(symbol) > 240
            or not path.endswith(".py")
        ):
            raise EngineeringWriterOutputError(
                "invalid engineering scoped symbol"
            )

        posix = PurePosixPath(path)
        if posix.is_absolute() or ".." in posix.parts:
            raise EngineeringWriterOutputError(
                "engineering edit path traversal is forbidden"
            )
        normalized = posix.as_posix()
        if normalized in mapped:
            raise EngineeringWriterOutputError(
                "duplicate engineering edit path"
            )

        encoded = content.encode("utf-8")
        if len(encoded) > MAX_EDIT_FILE_BYTES:
            raise EngineeringWriterOutputError(
                "engineering generated file exceeds limit"
            )
        total_bytes += len(encoded)
        if total_bytes > MAX_GENERATED_PATCH_BYTES:
            raise EngineeringWriterOutputError(
                "engineering generated patch exceeds limit"
            )

        if symbol is None:
            mapped[normalized] = content
            metadata.append({
                "path": normalized,
                "reason": reason.strip(),
                "scope": "full_file",
            })
        else:
            mapped[normalized] = ScopedPythonSymbolEdit(
                symbol=symbol,
                content=content,
            )
            metadata.append({
                "path": normalized,
                "reason": reason.strip(),
                "scope": "python_symbol_block",
                "symbol": symbol,
            })

    return (
        summary.strip(),
        mapped,
        metadata,
        needs_more,
        clean_requests,
    )


def _existing_targets_missing_from_context(
    root: Path,
    edits: Mapping[str, object],
    project_context: Mapping[str, object],
    policy: Mapping[str, Any],
) -> list[str]:
    supplied = {
        row.get("path")
        for row in project_context.get("files", [])
        if isinstance(row, Mapping)
        and isinstance(row.get("path"), str)
    }
    missing: list[str] = []
    for relative in edits:
        if not _safe_context_relative(relative, policy):
            continue
        path = root / relative
        if (
            path.is_file()
            and not path.is_symlink()
            and relative not in supplied
            and relative not in missing
        ):
            missing.append(relative)
    return missing


class EngineeringResearchBackedWriter:
    """Generate one bounded structured engineering patch."""

    def __init__(
        self,
        root: Path,
        model: EngineeringPatchModel,
    ):
        self.root = Path(root).resolve()
        self.model = model
        self.last_report: dict[str, object] | None = None

    @staticmethod
    def _sum_optional(
        values: list[int | None],
    ) -> int | None:
        if any(value is None for value in values):
            return None
        return sum(int(value) for value in values)

    def _model_progress_report(
        self,
        passes: list[tuple[dict[str, object], CodeModelBatch]],
    ) -> dict[str, object]:
        """Record safe usage before validating untrusted model output."""
        contexts = [item[0] for item in passes]
        batches = [item[1] for item in passes]
        context = contexts[-1]
        return {
            "schema": "sira.engineering_writer_report.v1",
            "policy_version": ENGINEERING_WRITER_POLICY_VERSION,
            "status": "model_response_received",
            "model": {
                "provider": self.model.name,
                "id": self.model.model_id,
            },
            "context_sha256": context["context_sha256"],
            "context_file_count": context["context_file_count"],
            "context_bytes": context["context_bytes"],
            "context_pass_count": len(contexts),
            "api_requests": sum(item.api_requests for item in batches),
            "attempt_count": sum(item.attempt_count for item in batches),
            "retry_delays": [
                delay
                for item in batches
                for delay in item.retry_delays
            ],
            "input_tokens": self._sum_optional([
                item.input_tokens for item in batches
            ]),
            "output_tokens": self._sum_optional([
                item.output_tokens for item in batches
            ]),
            "raw_output_included": False,
            "candidate_only": True,
            "main_tree_modified": False,
            "promotion_authorized": False,
        }

    def propose_text_edits(
        self,
        task: Mapping[str, object],
    ) -> Mapping[str, str]:
        policy = build_engineering_edit_policy(
            self.root
        )
        if policy.get("status") != "ready":
            raise EngineeringWriterOutputError(
                "project has no supported engineering edit policy"
            )

        passes: list[
            tuple[dict[str, object], CodeModelBatch]
        ] = []

        context = build_engineering_writer_context(
            self.root,
            task,
        )
        model_context = compact_engineering_model_context(
            context
        )
        batch = self.model.generate_patch(
            model_context,
            _patch_schema_for_context(context),
        )
        if not isinstance(batch, CodeModelBatch):
            raise EngineeringWriterOutputError(
                "engineering model returned invalid batch"
            )
        passes.append((context, batch))
        self.last_report = self._model_progress_report(passes)

        (
            summary,
            edits,
            metadata,
            needs_more,
            requests,
        ) = _validate_model_patch(
            batch.data,
            policy,
            context.get("scoped_edit"),
            allow_implicit_whole_file_symbol=(
                _scoped_target_covers_complete_file(context)
            ),
        )

        missing = _existing_targets_missing_from_context(
            self.root,
            edits,
            context,
            policy,
        )

        expansion: list[str] = []
        for relative in [*requests, *missing]:
            if relative not in expansion:
                expansion.append(relative)

        if needs_more or missing:
            if not expansion:
                raise EngineeringWriterOutputError(
                    "engineering expansion has no usable paths"
                )
            expanded = build_engineering_writer_context(
                self.root,
                task,
                expanded_paths=expansion,
            )
            expanded_model_context = (
                compact_engineering_model_context(expanded)
            )
            second = self.model.generate_patch(
                expanded_model_context,
                _patch_schema_for_context(expanded),
            )
            if not isinstance(second, CodeModelBatch):
                raise EngineeringWriterOutputError(
                    "engineering model returned invalid expansion batch"
                )
            passes.append((expanded, second))
            self.last_report = self._model_progress_report(passes)

            (
                summary,
                edits,
                metadata,
                needs_more,
                requests,
            ) = _validate_model_patch(
                second.data,
                policy,
                expanded.get("scoped_edit"),
                allow_implicit_whole_file_symbol=(
                    _scoped_target_covers_complete_file(expanded)
                ),
            )
            if needs_more:
                raise EngineeringWriterOutputError(
                    "engineering model requested more than one expansion"
                )
            still_missing = _existing_targets_missing_from_context(
                self.root,
                edits,
                expanded,
                policy,
            )
            if still_missing:
                raise EngineeringWriterOutputError(
                    "existing engineering edit target lacks full context"
                )
            context = expanded

        try:
            materialized_edits = materialize_engineering_edits(
                self.root,
                edits,
                context,
                max_edit_file_bytes=MAX_EDIT_FILE_BYTES,
                max_patch_bytes=MAX_GENERATED_PATCH_BYTES,
            )
        except EngineeringScopedEditError as exc:
            raise EngineeringWriterOutputError(str(exc)) from None

        batches = [item[1] for item in passes]
        contexts = [item[0] for item in passes]

        self.last_report = {
            "schema": "sira.engineering_writer_report.v1",
            "policy_version": ENGINEERING_WRITER_POLICY_VERSION,
            "model": {
                "provider": self.model.name,
                "id": self.model.model_id,
            },
            "context_sha256": context[
                "context_sha256"
            ],
            "context_file_count": context[
                "context_file_count"
            ],
            "context_bytes": context[
                "context_bytes"
            ],
            "context_pass_count": len(contexts),
            "context_total_bytes": sum(
                int(item["context_bytes"])
                for item in contexts
            ),
            "context_passes": [
                {
                    "tier": item["context_tier"],
                    "sha256": item[
                        "context_sha256"
                    ],
                    "file_count": item[
                        "context_file_count"
                    ],
                    "bytes": item["context_bytes"],
                    "requested_paths": item[
                        "requested_paths"
                    ],
                }
                for item in contexts
            ],
            "summary": summary,
            "edit_metadata": metadata,
            "api_requests": sum(
                item.api_requests
                for item in batches
            ),
            "attempt_count": sum(
                item.attempt_count
                for item in batches
            ),
            "retry_delays": [
                delay
                for item in batches
                for delay in item.retry_delays
            ],
            "input_tokens": self._sum_optional(
                [
                    item.input_tokens
                    for item in batches
                ]
            ),
            "output_tokens": self._sum_optional(
                [
                    item.output_tokens
                    for item in batches
                ]
            ),
            "candidate_only": True,
            "main_tree_modified": False,
            "promotion_authorized": False,
        }
        return materialized_edits


def _merge_verification_plans(
    plans: Mapping[str, object],
) -> dict[str, Any] | None:
    if not isinstance(plans, Mapping):
        raise EngineeringWriterOutputError(
            "engineering verification plans are invalid"
        )

    commands: list[dict[str, Any]] = []
    identities: dict[
        str,
        tuple[object, ...],
    ] = {}

    for target in sorted(plans):
        plan = plans[target]
        if (
            not isinstance(plan, Mapping)
            or plan.get("schema")
            != "sira.engineering_verification_plan.v1"
            or plan.get("candidate_only_required")
            is not True
            or plan.get("network_disabled_required")
            is not True
            or plan.get("execution_performed")
            is not False
        ):
            raise EngineeringWriterOutputError(
                "invalid engineering verification plan"
            )

        rows = plan.get("commands")
        if not isinstance(rows, list):
            raise EngineeringWriterOutputError(
                "invalid engineering verification commands"
            )

        for row in rows:
            if not isinstance(row, Mapping):
                raise EngineeringWriterOutputError(
                    "invalid engineering verification row"
                )
            command_id = row.get("command_id")
            if not isinstance(command_id, str):
                raise EngineeringWriterOutputError(
                    "engineering verification command lacks id"
                )

            identity = (
                row.get("kind"),
                row.get("language"),
                tuple(row.get("argv", []))
                if isinstance(row.get("argv"), list)
                else None,
                tuple(
                    sorted(
                        row.get("env", {}).items()
                    )
                )
                if isinstance(
                    row.get("env"),
                    Mapping,
                )
                else None,
                row.get("available"),
                row.get("candidate_only"),
                row.get("network_policy"),
                row.get("shell"),
                row.get("authority_granted"),
                row.get("promotion_authorized"),
            )

            prior = identities.get(command_id)
            if prior is not None:
                if prior != identity:
                    raise EngineeringWriterOutputError(
                        "conflicting verification definitions"
                    )
                continue

            identities[command_id] = identity
            commands.append(dict(row))

    if not commands:
        return None
    if len(commands) > MAX_VERIFICATION_COMMANDS:
        raise EngineeringWriterOutputError(
            "merged verification plan exceeds command limit"
        )

    return {
        "schema": "sira.engineering_verification_plan.v1",
        "policy_version": 1,
        "target_path": None,
        "target_language": "multi",
        "commands": commands,
        "command_count": len(commands),
        "available_command_count": sum(
            row.get("available") is True
            for row in commands
        ),
        "execution_performed": False,
        "install_performed": False,
        "network_requests": 0,
        "candidate_only_required": True,
        "network_disabled_required": True,
        "paid_spending": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }


def prepare_verified_engineering_candidate(
    root: Path,
    task: Mapping[str, object],
    workspace: Path,
    writer: EngineeringResearchBackedWriter,
    *,
    command_runner=None,
    bubblewrap_path: str | None = None,
) -> dict[str, object]:
    """Generate, apply and verify one candidate without promotion."""
    root = Path(root).resolve()
    workspace = Path(workspace).absolute()

    if not root.is_dir():
        raise ValueError(
            "engineering project root must exist"
        )
    if workspace.exists() or workspace.is_symlink():
        raise ValueError(
            "engineering writer workspace must not already exist"
        )
    try:
        workspace.resolve(
            strict=False
        ).relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError(
            "engineering writer workspace must be outside main tree"
        )

    workspace.mkdir(
        parents=True,
        mode=0o700,
        exist_ok=False,
    )

    result: dict[str, object] = {
        "schema": "sira.engineering_writer_attempt.v1",
        "policy_version": ENGINEERING_WRITER_POLICY_VERSION,
        "created_at": utc_now(),
        "task_id": task.get("task_id"),
        "workspace": str(workspace),
        "candidate_root": None,
        "status": "no_edit_generated",
        "writer_report": {},
        "error": None,
        "edit_candidate": None,
        "verification_plan": None,
        "verification": None,
        "diagnostic_advisory": None,
        "verification_executed": False,
        "main_tree_modified": False,
        "package_installation_performed": False,
        "authority_granted": False,
        "promotion_performed": False,
        "promotion_authorized": False,
    }

    try:
        edits = writer.propose_text_edits(task)
    except EngineeringWriterOutputError as exc:
        writer_report = dict(writer.last_report or {})
        writer_report.update({
            "status": "model_output_rejected",
            "error_code": exc.code,
            "raw_output_included": False,
        })
        result.update({
            "status": "model_output_rejected",
            "writer_report": writer_report,
            "error": {
                "type": "EngineeringWriterOutputError",
                "code": exc.code,
                "raw_output_included": False,
            },
        })
        write_json(
            workspace / "engineering_writer_attempt.json",
            result,
        )
        return result

    result["writer_report"] = writer.last_report or {}

    if not edits:
        write_json(
            workspace
            / "engineering_writer_attempt.json",
            result,
        )
        return result

    edit_workspace = workspace / "edit"
    edit_candidate = prepare_engineering_edit_candidate(
        root,
        edit_workspace,
        edits,
    )
    candidate_root = Path(
        str(edit_candidate["candidate_root"])
    )
    merged_plan = _merge_verification_plans(
        edit_candidate["verification_plans"]
    )

    result.update({
        "candidate_root": str(candidate_root),
        "edit_candidate": edit_candidate,
        "verification_plan": merged_plan,
    })

    if merged_plan is None:
        result["status"] = "verification_unavailable"
        result["diagnostic_advisory"] = {
            "schema":
                "sira.engineering_diagnostic_advisory.v1",
            "diagnostics": [],
            "diagnostic_count": 0,
            "fingerprint_counts": {},
            "repair_authority": "advisory_only",
            "raw_output_included": False,
            "authority_granted": False,
            "promotion_authorized": False,
        }
        write_json(
            workspace
            / "engineering_writer_attempt.json",
            result,
        )
        return result

    verification = execute_verification_plan(
        candidate_root,
        merged_plan,
        main_root=root,
        command_runner=command_runner,
        bubblewrap_path=bubblewrap_path,
    )
    advisory = diagnostic_advisory_context(
        verification
    )

    if verification.get("overall_passed") is True:
        status = "verified_candidate_ready"
    elif verification.get("status") == "blocked":
        status = "verification_blocked"
    else:
        status = "candidate_rejected"

    result.update({
        "status": status,
        "verification": verification,
        "diagnostic_advisory": advisory,
        "verification_executed": (
            int(
                verification.get(
                    "processes_executed",
                    0,
                )
            )
            > 0
        ),
    })

    write_json(
        workspace / "engineering_writer_attempt.json",
        result,
    )
    return result


def make_default_engineering_writer(
    root: Path,
) -> EngineeringResearchBackedWriter:
    """Construct the isolated Gemini engineering writer."""
    from .config import load_key
    from .providers.gemini_engineering import (
        GeminiEngineeringModel,
    )

    root = Path(root).resolve()
    return EngineeringResearchBackedWriter(
        root,
        GeminiEngineeringModel(
            load_key(root, "GEMINI_API_KEY")
        ),
    )
