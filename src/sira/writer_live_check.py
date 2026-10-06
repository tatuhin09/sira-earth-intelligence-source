"""Controlled live diagnostic for the configured autonomous code writer.

This command intentionally never promotes a candidate and never touches learning
memory. It exercises the real writer, candidate boundary, Evaluator 2 and
Evaluator 1 using one exact docs-only edit in an isolated runtime workspace.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Protocol
from uuid import uuid4

from .code_writer import ResearchBackedCodeWriter, make_default_code_writer
from .evaluator1 import evaluate_final_gate
from .evaluator2 import evaluate_candidate
from .improvement import ExperimentRunner, SubprocessExperimentRunner
from .models import utc_now
from .self_modification import CandidateEditError, ValidatedCandidateEditor, prepare_candidate_workspace
from .storage import write_json

WRITER_LIVE_CHECK_POLICY_VERSION = 1
LIVE_CHECK_PATH = "docs/writer_live_check_candidate.md"
LIVE_CHECK_MARKER = "SIRA_WRITER_LIVE_CHECK_OK"
MAX_LIVE_CHECK_CONTENT_BYTES = 4096


class LiveCheckWriter(Protocol):
    last_report: dict[str, object] | None

    def propose_text_edits(self, context: Mapping[str, object]) -> Mapping[str, str]: ...


def _diagnostic_hypothesis() -> dict[str, object]:
    return {
        "hypothesis_id": "live_writer_check",
        "memory_id": None,
        "benchmark_suite": "memory",
        "statement": (
            "Live writer diagnostic only: create exactly docs/writer_live_check_candidate.md "
            "with a short Markdown document containing the literal marker "
            f"{LIVE_CHECK_MARKER}. Do not edit any other file."
        ),
        "rationale": (
            "Exercise the configured code model and verification gates without promotion or learning-memory changes."
        ),
        "memory_snapshot": {
            "kind": "diagnostic",
            "category": "writer_live_check",
            "capability": "memory",
            "provider": "configured_code_writer",
            "error_code": None,
            "summary": "controlled docs-only autonomous writer diagnostic",
            "occurrence_count": 1,
            "status": "diagnostic",
        },
        "related_memories": [],
    }


def _safe_error(exc: Exception) -> dict[str, object]:
    code = getattr(exc, "code", None)
    requests = getattr(exc, "request_count", None)
    raw_delays = getattr(exc, "retry_delays", ())
    retry_delays = [
        float(value) for value in raw_delays
        if type(value) in (int, float) and 0 <= float(value) <= 5.0
    ] if isinstance(raw_delays, (tuple, list)) else []
    return {
        "type": type(exc).__name__,
        "code": str(code) if isinstance(code, str) else None,
        "api_requests": requests if type(requests) is int and requests >= 0 else 0,
        "retry_delays": retry_delays,
    }


def _validate_diagnostic_edits(edits: Mapping[str, str]) -> tuple[bool, str]:
    if not isinstance(edits, Mapping) or set(edits) != {LIVE_CHECK_PATH}:
        return False, "live check requires exactly one diagnostic docs edit"
    content = edits.get(LIVE_CHECK_PATH)
    if not isinstance(content, str) or LIVE_CHECK_MARKER not in content:
        return False, "diagnostic marker missing from generated content"
    if len(content.encode("utf-8")) > MAX_LIVE_CHECK_CONTENT_BYTES:
        return False, "diagnostic content exceeds live-check size limit"
    return True, "ok"


def run_writer_live_check(
    root: Path,
    *,
    writer: LiveCheckWriter | None = None,
    runner: ExperimentRunner | None = None,
) -> dict[str, object]:
    """Exercise the real writer + both evaluators without promotion or memory mutation."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("project root must exist")

    check_id = "wlc_" + uuid4().hex
    workspace = root / "runtime" / "writer_live_checks" / check_id
    candidate_root = workspace / "candidate"
    workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
    artifact = workspace / "writer_live_check.json"
    hypothesis = _diagnostic_hypothesis()
    writer = writer or make_default_code_writer(root)
    runner = runner or SubprocessExperimentRunner()

    result: dict[str, object] = {
        "schema_version": 1,
        "kind": "writer_live_check",
        "policy_version": WRITER_LIVE_CHECK_POLICY_VERSION,
        "check_id": check_id,
        "created_at": utc_now(),
        "status": "writer_error",
        "writer_status": "not_started",
        "workspace": str(workspace),
        "candidate_root": None,
        "diagnostic_path": LIVE_CHECK_PATH,
        "diagnostic_marker": LIVE_CHECK_MARKER,
        "writer_report": None,
        "baseline": None,
        "candidate": None,
        "evaluator2": None,
        "evaluator1": None,
        "promotion_performed": False,
        "main_tree_modified": False,
        "memory_modified": False,
        "error": None,
        "artifact": str(artifact),
    }

    try:
        edits = writer.propose_text_edits(hypothesis)
        raw_report = getattr(writer, "last_report", None)
        result["writer_report"] = dict(raw_report) if isinstance(raw_report, Mapping) else None
        valid, reason = _validate_diagnostic_edits(edits)
        if not valid:
            result.update({"status": "rejected_writer_scope", "writer_status": "rejected_scope", "error": reason})
            write_json(artifact, result)
            return result

        manifest = prepare_candidate_workspace(root, candidate_root)
        edit_report = ValidatedCandidateEditor().apply_text_edits(candidate_root, edits)
        result.update({
            "writer_status": "candidate_prepared",
            "candidate_root": str(candidate_root),
            "candidate_manifest": manifest,
            "edit_report": edit_report,
        })

        baseline = runner.evaluate(root, "memory")
        candidate = runner.evaluate(candidate_root, "memory")
        evaluator2 = evaluate_candidate(root, candidate_root, baseline, candidate)
        result.update({"baseline": baseline, "candidate": candidate, "evaluator2": evaluator2})

        if evaluator2.get("decision") != "accept" or evaluator2.get("decision_code") != "candidate_verified":
            result["status"] = "failed_evaluator2"
            write_json(artifact, result)
            return result

        evaluator1 = evaluate_final_gate(root, candidate_root, evaluator2, baseline, candidate)
        result["evaluator1"] = evaluator1
        if evaluator1.get("decision") != "allow" or evaluator1.get("decision_code") != "promotion_authorized":
            result["status"] = "failed_evaluator1"
            write_json(artifact, result)
            return result

        result["status"] = "passed"
        write_json(artifact, result)
        return result
    except (CandidateEditError, ValueError, OSError, RuntimeError) as exc:
        result["error"] = _safe_error(exc)
        write_json(artifact, result)
        return result
    except Exception as exc:
        # ProviderError and transport-layer failures are intentionally reduced to
        # bounded metadata. No raw response bodies, credentials or tracebacks are persisted.
        result["error"] = _safe_error(exc)
        write_json(artifact, result)
        return result
