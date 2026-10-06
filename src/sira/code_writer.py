"""Bounded research-backed autonomous code generation for isolated candidates.

v1.0C-1 stops before evaluation or promotion. It builds a bounded project
context, asks a structured code model for full-text replacements, validates the
model shape, then applies edits only through the existing candidate boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping, Protocol

from .models import utc_now
from .self_modification import (
    MAX_EDIT_FILE_BYTES,
    PROTECTED_PATHS,
    ValidatedCandidateEditor,
    prepare_candidate_workspace,
)
from .storage import write_json

CODE_WRITER_POLICY_VERSION = 2
PRIMARY_CONTEXT_FILE_LIMIT = 6
PRIMARY_CONTEXT_TOTAL_BYTES = 48 * 1024
EXPANDED_CONTEXT_FILE_LIMIT = 8
EXPANDED_CONTEXT_TOTAL_BYTES = 160 * 1024
MAX_CONTEXT_FILE_BYTES = 96 * 1024
MAX_CONTEXT_INDEX_FILES = 40
MAX_CONTEXT_REQUESTS = 4
MAX_GENERATED_EDITS = 8
MAX_GENERATED_REASON_CHARS = 1000
MAX_GENERATED_SUMMARY_CHARS = 2000
MAX_HYPOTHESIS_JSON_BYTES = 24 * 1024
_ALLOWED_CONTEXT_SUFFIXES = frozenset({".py", ".json", ".md", ".txt"})
_CONTEXT_ROOTS = ("src/sira", "tests", "benchmarks", "docs")
_WORD = re.compile(r"[A-Za-z0-9_]{3,}")
_EXPLICIT_PATH = re.compile(r"(?<![A-Za-z0-9_./-])((?:src/sira|tests|benchmarks|docs)/[A-Za-z0-9_./-]+\.(?:py|json|md|txt))(?![A-Za-z0-9_./-])")

CAPABILITY_FILE_HINTS = {
    "scholarly_research": (
        "src/sira/papers.py",
        "src/sira/providers/semantic_scholar.py",
        "src/sira/providers/crossref.py",
        "src/sira/providers/arxiv.py",
        "tests/test_papers.py",
        "tests/test_paper_fallback.py",
        "tests/test_arxiv_enrichment.py",
        "benchmarks/paper_cases.json",
    ),
    "paper_reading": (
        "src/sira/paper_reading.py",
        "src/sira/providers/pdf_text.py",
        "tests/test_paper_reading.py",
        "benchmarks/paper_reading_cases.json",
    ),
    "reading": (
        "src/sira/reading.py",
        "src/sira/providers/tavily_extract.py",
        "tests/test_reading.py",
        "benchmarks/reading_cases.json",
    ),
    "retrieval": (
        "src/sira/retrieval.py",
        "src/sira/providers/tavily.py",
        "tests/test_sira.py",
        "benchmarks/retrieval_cases.json",
    ),
    "synthesis": (
        "src/sira/synthesis.py",
        "src/sira/providers/gemini.py",
        "tests/test_synthesis.py",
        "benchmarks/synthesis_cases.json",
    ),
    "memory": (
        "src/sira/memory.py",
        "tests/test_memory.py",
        "benchmarks/memory_cases.json",
    ),
}

CODE_PATCH_SCHEMA = {
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


@dataclass(frozen=True)
class CodeModelBatch:
    data: dict
    api_requests: int = 0
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    attempt_count: int = 1
    retry_delays: tuple[float, ...] = ()


class CodePatchModel(Protocol):
    name: str
    model_id: str

    def generate_patch(self, payload: dict, schema: dict) -> CodeModelBatch: ...


class CodeWriterOutputError(ValueError):
    """Raised when a code model returns a malformed structured patch."""


def _bounded_hypothesis(hypothesis: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(hypothesis, Mapping):
        raise ValueError("hypothesis must be a mapping")
    snapshot = hypothesis.get("memory_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    related = hypothesis.get("related_memories")
    related = related if isinstance(related, list) else []
    opportunity_context = hypothesis.get("opportunity_context")
    opportunity_context = opportunity_context if isinstance(opportunity_context, Mapping) else {}
    target = hypothesis.get("target")
    target = target if isinstance(target, Mapping) else {}
    success_criteria = hypothesis.get("success_criteria")
    success_criteria = success_criteria if isinstance(success_criteria, Mapping) else {}
    citations = opportunity_context.get("citations")
    citations = citations if isinstance(citations, list) else []
    strategies = opportunity_context.get("candidate_strategies")
    strategies = strategies if isinstance(strategies, list) else []
    result: dict[str, object] = {
        "hypothesis_id": hypothesis.get("hypothesis_id"),
        "memory_id": hypothesis.get("memory_id"),
        "benchmark_suite": hypothesis.get("benchmark_suite"),
        "statement": hypothesis.get("statement"),
        "rationale": hypothesis.get("rationale"),
        "target": {key: target.get(key) for key in ("path", "symbol")},
        "success_criteria": {"structural_goal": success_criteria.get("structural_goal")},
        "opportunity_context": {
            "opportunity_id": opportunity_context.get("opportunity_id"),
            "research_id": opportunity_context.get("research_id"),
            "citations": [
                {key: row.get(key) for key in ("citation_id", "provider", "title", "url", "abstract_excerpt")}
                for row in citations[:4] if isinstance(row, Mapping)
            ],
            "candidate_strategies": [
                {"strategy": row.get("strategy"), "citation_ids": row.get("citation_ids")}
                for row in strategies[:4] if isinstance(row, Mapping)
            ],
        },
        "memory_snapshot": {key: snapshot.get(key) for key in (
            "kind", "category", "capability", "provider", "error_code", "summary",
            "occurrence_count", "status",
        )},
        "related_memories": [
            {key: row.get(key) for key in (
                "kind", "status", "category", "capability", "provider", "error_code", "summary",
            )}
            for row in related[:5] if isinstance(row, Mapping)
        ],
    }
    try:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("hypothesis contains non-JSON data") from None
    if len(encoded) > MAX_HYPOTHESIS_JSON_BYTES:
        raise ValueError("hypothesis context exceeds local size limit")
    return result


def _iter_context_files(root: Path):
    root = Path(root).resolve()
    for base in _CONTEXT_ROOTS:
        start = root / base
        if not start.is_dir() or start.is_symlink():
            continue
        for current, dirs, names in os.walk(start, followlinks=False):
            current_path = Path(current)
            dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()
                       and name not in {"__pycache__", ".cache", ".pytest_cache"}]
            for name in names:
                path = current_path / name
                if path.is_symlink() or not path.is_file() or path.suffix.lower() not in _ALLOWED_CONTEXT_SUFFIXES:
                    continue
                relative = path.relative_to(root).as_posix()
                if relative in PROTECTED_PATHS:
                    continue
                yield relative, path


def _search_tokens(hypothesis: Mapping[str, object]) -> set[str]:
    bounded = _bounded_hypothesis(hypothesis)
    text = json.dumps(bounded, ensure_ascii=False).lower()
    return {match.group(0) for match in _WORD.finditer(text) if len(match.group(0)) >= 4}


def _file_score(relative: str, text: str, tokens: set[str], hints: tuple[str, ...]) -> int:
    relative_lower = relative.lower()
    text_lower = text.lower()
    score = 0
    if relative in hints:
        score += 1000 - hints.index(relative)
    for token in tokens:
        if token in relative_lower:
            score += 30
        occurrences = text_lower.count(token)
        if occurrences:
            score += min(occurrences, 4) * 2
    if relative.startswith("src/sira/"):
        score += 5
    elif relative.startswith("tests/"):
        score += 4
    elif relative.startswith("benchmarks/"):
        score += 3
    return score


def _explicit_paths(hypothesis: Mapping[str, object]) -> list[str]:
    bounded = _bounded_hypothesis(hypothesis)
    haystack = "\n".join(str(bounded.get(key) or "") for key in ("statement", "rationale"))
    found: list[str] = []
    for match in _EXPLICIT_PATH.finditer(haystack):
        path = match.group(1)
        if path not in found:
            found.append(path)
    return found[:MAX_CONTEXT_REQUESTS]


def _allowed_context_path(relative: str) -> bool:
    if not isinstance(relative, str) or not relative or len(relative) > 240:
        return False
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or relative != path.as_posix():
        return False
    if relative in PROTECTED_PATHS or path.suffix.lower() not in _ALLOWED_CONTEXT_SUFFIXES:
        return False
    return any(relative == root or relative.startswith(root + "/") for root in _CONTEXT_ROOTS)


def _read_context_file(root: Path, relative: str) -> tuple[str, int, str] | None:
    if not _allowed_context_path(relative):
        return None
    path = root / relative
    try:
        if path.is_symlink() or not path.is_file():
            return None
        payload = path.read_bytes()
    except OSError:
        return None
    if len(payload) > MAX_CONTEXT_FILE_BYTES or b"\x00" in payload:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text, len(payload), hashlib.sha256(payload).hexdigest()


def _index_context_files(
    root: Path, tokens: set[str], hints: tuple[str, ...], explicit: list[str],
) -> tuple[list[tuple[int, str, str, int, str]], list[dict[str, object]]]:
    """Index permitted files and retain only readable, relevant context candidates."""
    candidates: list[tuple[int, str, str, int, str]] = []
    index_rows: list[dict[str, object]] = []
    for relative, path in _iter_context_files(root):
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in payload:
            continue
        digest = hashlib.sha256(payload).hexdigest()
        size = len(payload)
        try:
            text = payload.decode("utf-8") if size <= MAX_CONTEXT_FILE_BYTES else ""
        except UnicodeDecodeError:
            continue
        score = _file_score(relative, text, tokens, hints)
        if relative in explicit:
            score += 5000
        index_rows.append({"path": relative, "bytes": size, "score": score, "sha256": digest})
        if text and (score > 0 or relative in hints or relative in explicit):
            candidates.append((score, relative, text, size, digest))
    return candidates, index_rows


def build_project_context(
    root: Path,
    hypothesis: Mapping[str, object],
    *,
    expanded_paths: tuple[str, ...] | list[str] | None = None,
) -> dict[str, object]:
    """Build a focused first-pass context or one bounded expansion pass."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("project root must exist")
    bounded_hypothesis = _bounded_hypothesis(hypothesis)
    snapshot = bounded_hypothesis.get("memory_snapshot")
    capability = snapshot.get("capability") if isinstance(snapshot, Mapping) else None
    hints = CAPABILITY_FILE_HINTS.get(str(capability), ())
    tokens = _search_tokens(hypothesis)
    explicit = _explicit_paths(hypothesis)

    candidates, index_rows = _index_context_files(root, tokens, hints, explicit)

    index_rows.sort(key=lambda row: (-int(row["score"]), str(row["path"])))
    available_index = index_rows[:MAX_CONTEXT_INDEX_FILES]

    requested: list[str] = []
    if expanded_paths is not None:
        for relative in expanded_paths:
            if not _allowed_context_path(relative):
                raise CodeWriterOutputError("invalid or protected context request")
            if relative not in requested:
                requested.append(relative)
        if not requested or len(requested) > MAX_CONTEXT_REQUESTS:
            raise CodeWriterOutputError("invalid context expansion request")
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
        # Keep the focused first-pass files too when budget permits so the second
        # call has the original local evidence plus the specifically requested file.
        ordered.extend(row[1] for row in sorted(candidates, key=lambda item: (-item[0], item[1])))
        seen: set[str] = set()
        for relative in ordered:
            if relative in seen:
                continue
            seen.add(relative)
            loaded = _read_context_file(root, relative)
            if loaded is None:
                if relative in requested:
                    raise CodeWriterOutputError("requested context file is unavailable or too large")
                continue
            content, size, digest = loaded
            if len(selected) >= file_limit or total_bytes + size > byte_limit:
                if relative in requested:
                    raise CodeWriterOutputError("requested context exceeds bounded expansion budget")
                continue
            selected.append({"path": relative, "sha256": digest, "content": content})
            total_bytes += size
    elif explicit:
        # Explicit path tasks are narrow. Existing targets are supplied in full;
        # new-file tasks intentionally send no unrelated source content.
        for relative in explicit:
            loaded = _read_context_file(root, relative)
            if loaded is None:
                continue
            content, size, digest = loaded
            if len(selected) >= file_limit or total_bytes + size > byte_limit:
                continue
            selected.append({"path": relative, "sha256": digest, "content": content})
            total_bytes += size
    else:
        for _, relative, content, size, digest in sorted(candidates, key=lambda item: (-item[0], item[1])):
            if len(selected) >= file_limit or total_bytes + size > byte_limit:
                continue
            selected.append({"path": relative, "sha256": digest, "content": content})
            total_bytes += size

    base = {
        "policy_version": CODE_WRITER_POLICY_VERSION,
        "context_tier": tier,
        "hypothesis": bounded_hypothesis,
        "files": selected,
        "context_file_count": len(selected),
        "context_bytes": total_bytes,
        "context_budget_bytes": byte_limit,
        "explicit_paths": explicit,
        "requested_paths": requested,
        "available_context_index": available_index,
        "modifiable_roots": ["src/sira", "tests", "benchmarks", "docs"],
        "protected_paths": sorted(PROTECTED_PATHS),
        "output_contract": (
            "complete UTF-8 file replacements keyed by project-relative POSIX path; "
            "request one bounded context expansion before editing an existing file whose full content is absent"
        ),
    }
    canonical = json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**base, "context_sha256": hashlib.sha256(canonical).hexdigest()}


def _validate_model_patch(
    value: object,
) -> tuple[str, dict[str, str], list[dict[str, str]], bool, list[str]]:
    if not isinstance(value, dict):
        raise CodeWriterOutputError("code model output must be an object")
    allowed_keys = {"summary", "edits", "needs_more_context", "context_requests"}
    if not {"summary", "edits"}.issubset(value) or not set(value).issubset(allowed_keys):
        raise CodeWriterOutputError("invalid code model output keys")
    summary = value.get("summary")
    edits = value.get("edits")
    needs_more = value.get("needs_more_context", False)
    requests = value.get("context_requests", [])
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_GENERATED_SUMMARY_CHARS:
        raise CodeWriterOutputError("invalid code model summary")
    if not isinstance(edits, list) or len(edits) > MAX_GENERATED_EDITS:
        raise CodeWriterOutputError("invalid generated edit list")
    if type(needs_more) is not bool:
        raise CodeWriterOutputError("needs_more_context must be boolean")
    if not isinstance(requests, list) or len(requests) > MAX_CONTEXT_REQUESTS:
        raise CodeWriterOutputError("invalid context request list")

    clean_requests: list[str] = []
    for relative in requests:
        if not isinstance(relative, str) or not _allowed_context_path(relative):
            raise CodeWriterOutputError("invalid or protected context request")
        if relative not in clean_requests:
            clean_requests.append(relative)
    if needs_more and edits:
        raise CodeWriterOutputError("context expansion request cannot include edits")
    if needs_more and not clean_requests:
        raise CodeWriterOutputError("context expansion requires explicit file requests")

    mapped: dict[str, str] = {}
    metadata: list[dict[str, str]] = []
    total_bytes = 0
    for item in edits:
        if not isinstance(item, dict) or set(item) != {"path", "content", "reason"}:
            raise CodeWriterOutputError("invalid generated edit item")
        path, content, reason = item.get("path"), item.get("content"), item.get("reason")
        if (not isinstance(path, str) or not path or len(path) > 240
                or not isinstance(content, str) or "\x00" in content
                or not isinstance(reason, str) or not reason.strip()
                or len(reason) > MAX_GENERATED_REASON_CHARS):
            raise CodeWriterOutputError("invalid generated edit fields")
        if path in mapped:
            raise CodeWriterOutputError("duplicate generated edit path")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_EDIT_FILE_BYTES:
            raise CodeWriterOutputError("generated file exceeds candidate size limit")
        total_bytes += len(encoded)
        if total_bytes > 512 * 1024:
            raise CodeWriterOutputError("generated patch exceeds writer size limit")
        mapped[path] = content
        metadata.append({"path": path, "reason": reason})
    return summary.strip(), mapped, metadata, needs_more, clean_requests


class ResearchBackedCodeWriter:
    """Generate one structured patch with a focused pass and at most one expansion."""

    def __init__(self, root: Path, model: CodePatchModel):
        self.root = Path(root).resolve()
        self.model = model
        self.last_report: dict[str, object] | None = None

    @staticmethod
    def _sum_optional(values: list[int | None]) -> int | None:
        return None if any(value is None for value in values) else sum(int(value) for value in values)

    def _existing_targets_missing_from_context(
        self, edits: Mapping[str, str], project_context: Mapping[str, object]
    ) -> list[str]:
        supplied = {
            row.get("path") for row in project_context.get("files", [])
            if isinstance(row, Mapping) and isinstance(row.get("path"), str)
        }
        missing: list[str] = []
        for relative in edits:
            if not _allowed_context_path(relative):
                continue
            path = self.root / relative
            if path.is_file() and relative not in supplied and relative not in missing:
                missing.append(relative)
        return missing

    def propose_text_edits(self, context: Mapping[str, object]) -> Mapping[str, str]:
        passes: list[tuple[dict[str, object], CodeModelBatch]] = []
        project_context = build_project_context(self.root, context)

        def _run_pass(ctx: dict[str, object]) -> tuple[dict[str, object], CodeModelBatch, tuple[str, list[dict[str, object]], dict[str, object], bool, list[str]]]:
            batch = self.model.generate_patch(ctx, CODE_PATCH_SCHEMA)
            if not isinstance(batch, CodeModelBatch):
                raise CodeWriterOutputError("code model returned an invalid batch")
            return ctx, batch, _validate_model_patch(batch.data)

        project_context, batch, (summary, edits, metadata, needs_more, requests) = _run_pass(project_context)
        passes.append((project_context, batch))

        missing_targets = self._existing_targets_missing_from_context(edits, project_context)
        expansion_paths = list(dict.fromkeys([*requests, *missing_targets]))

        if needs_more or missing_targets:
            if not expansion_paths:
                raise CodeWriterOutputError("bounded context expansion has no usable paths")
            expanded = build_project_context(self.root, context, expanded_paths=expansion_paths)
            project_context, batch, (summary, edits, metadata, needs_more, _) = _run_pass(expanded)
            if needs_more:
                raise CodeWriterOutputError("code model requested more than one bounded context expansion")
            if self._existing_targets_missing_from_context(edits, expanded):
                raise CodeWriterOutputError("generated edit targets were not supplied in full context")
            passes.append((expanded, batch))

        batches = [item[1] for item in passes]
        contexts = [item[0] for item in passes]
        self.last_report = {
            "policy_version": CODE_WRITER_POLICY_VERSION,
            "model": {"provider": self.model.name, "id": self.model.model_id},
            "context_sha256": project_context["context_sha256"],
            "context_file_count": project_context["context_file_count"],
            "context_bytes": project_context["context_bytes"],
            "context_pass_count": len(contexts),
            "context_total_bytes": sum(int(item["context_bytes"]) for item in contexts),
            "context_passes": [
                {
                    "tier": item["context_tier"],
                    "sha256": item["context_sha256"],
                    "file_count": item["context_file_count"],
                    "bytes": item["context_bytes"],
                    "requested_paths": item["requested_paths"],
                }
                for item in contexts
            ],
            "summary": summary,
            "edit_metadata": metadata,
            "api_requests": sum(item.api_requests for item in batches),
            "attempt_count": sum(item.attempt_count for item in batches),
            "retry_delays": [delay for item in batches for delay in item.retry_delays],
            "input_tokens": self._sum_optional([item.input_tokens for item in batches]),
            "output_tokens": self._sum_optional([item.output_tokens for item in batches]),
        }
        return edits


def prepare_code_candidate(
    root: Path,
    hypothesis: Mapping[str, object],
    workspace: Path,
    writer: ResearchBackedCodeWriter,
) -> dict[str, object]:
    """Generate/apply one code proposal to an isolated candidate, never main."""
    root = Path(root).resolve()
    workspace = Path(workspace).absolute()
    if not root.is_dir():
        raise ValueError("project root must exist")
    if workspace.exists() or workspace.is_symlink():
        raise ValueError("writer workspace must not already exist")
    workspace.mkdir(parents=True, mode=0o700)

    edits = writer.propose_text_edits(hypothesis)
    report = writer.last_report or {}
    result: dict[str, object] = {
        "schema_version": 1,
        "kind": "autonomous_code_writer_attempt",
        "policy_version": CODE_WRITER_POLICY_VERSION,
        "created_at": utc_now(),
        "hypothesis_id": hypothesis.get("hypothesis_id"),
        "memory_id": hypothesis.get("memory_id"),
        "workspace": str(workspace),
        "candidate_root": None,
        "status": "no_edit_generated",
        "main_tree_modified": False,
        "promotion_performed": False,
        "model": report.get("model"),
        "context_sha256": report.get("context_sha256"),
        "context_file_count": report.get("context_file_count"),
        "context_bytes": report.get("context_bytes"),
        "summary": report.get("summary"),
        "edit_metadata": report.get("edit_metadata", []),
        "api_requests": report.get("api_requests", 0),
        "attempt_count": report.get("attempt_count", 1),
        "retry_delays": report.get("retry_delays", []),
        "input_tokens": report.get("input_tokens", 0),
        "output_tokens": report.get("output_tokens", 0),
        "candidate_manifest": None,
        "edit_report": None,
    }
    if edits:
        candidate_root = workspace / "candidate"
        manifest = prepare_candidate_workspace(root, candidate_root)
        edit_report = ValidatedCandidateEditor().apply_text_edits(candidate_root, edits)
        result.update({
            "candidate_root": str(candidate_root),
            "status": "candidate_prepared",
            "candidate_manifest": manifest,
            "edit_report": edit_report,
        })
    write_json(workspace / "code_writer_attempt.json", result)
    return result


def make_default_code_writer(root: Path) -> ResearchBackedCodeWriter:
    """Construct the configured Gemini writer without exposing its key to context."""
    from .config import load_key
    from .providers.gemini_code import GeminiCodeModel

    root = Path(root).resolve()
    return ResearchBackedCodeWriter(root, GeminiCodeModel(load_key(root, "GEMINI_API_KEY")))
