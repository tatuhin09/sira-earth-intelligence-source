"""Bounded autonomous runtime bridge for SIRA's protected engineering chain.

v1.8I connects an already-selected, research-ready opportunity to the existing
v1.8E -> v1.8H engineering pipeline.  It does not create new authority:
the writer remains candidate-only, the evaluator remains advisory, the
authorization gate remains checksum-only, and all main-tree writes still occur
only inside the protected v1.8H transactional promoter.

The runtime bridge is deliberately fail-closed and keeps a compatibility
fallback in runtime.py for opportunities that are not eligible for this path.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from uuid import uuid4

from .engineering_authorization import evaluate_engineering_authorization
from .autonomous_promotion import _structural_goal_check
from .engineering_editing import (
    SUPPORTED_EDIT_SUFFIXES,
    build_engineering_edit_policy,
)
from .engineering_evaluator import evaluate_engineering_candidate
from .engineering_promotion import promote_engineering_candidate
from .engineering_writer import (
    make_default_engineering_writer,
    prepare_verified_engineering_candidate,
)
from .models import utc_now
from .opportunity import OpportunityStore
from .self_modification import PROTECTED_PATHS
from .storage import write_json

ENGINEERING_RUNTIME_POLICY_VERSION = 1
MAX_RUNTIME_INSTRUCTION_CHARS = 5000
MAX_RESEARCH_STRATEGIES = 4
MAX_RESEARCH_CITATIONS = 4
MAX_RESEARCH_TEXT_CHARS = 900

RuntimeGuard = Callable[[], bool]


def _safe_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text[:limit]


def _target_path(
    target: Mapping[str, object],
    evidence: Mapping[str, object],
) -> str | None:
    opportunity = (
        evidence.get("opportunity")
        if isinstance(evidence.get("opportunity"), Mapping)
        else {}
    )
    value = opportunity.get("path") or target.get("path")
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def engineering_runtime_eligible(
    root: Path,
    target: Mapping[str, object],
    evidence: Mapping[str, object],
    research: Mapping[str, object],
) -> bool:
    """Return whether the existing opportunity can use the engineering path."""
    if not all(
        isinstance(value, Mapping)
        for value in (target, evidence, research)
    ):
        return False
    if target.get("target_kind") != "opportunity":
        return False

    opportunity = (
        evidence.get("opportunity")
        if isinstance(evidence.get("opportunity"), Mapping)
        else {}
    )
    if (
        opportunity.get("opportunity_id")
        and opportunity.get("opportunity_id") != target.get("opportunity_id")
    ):
        return False

    assessment = (
        evidence.get("assessment")
        if isinstance(evidence.get("assessment"), Mapping)
        else {}
    )
    quality = (
        research.get("research_quality")
        if isinstance(research.get("research_quality"), Mapping)
        else {}
    )
    if assessment.get("decision") != "research_ready":
        return False
    if research.get("writer_handoff_allowed") is not True:
        return False
    if quality.get("decision") != "writer_ready":
        return False
    if research.get("paid_spending") is not False:
        return False
    if research.get("metered_model_requests") != 0:
        return False

    relative = _target_path(target, evidence)
    if relative is None or relative in PROTECTED_PATHS:
        return False

    posix = PurePosixPath(relative)
    if posix.suffix.casefold() not in SUPPORTED_EDIT_SUFFIXES:
        return False

    root = Path(root).resolve()
    try:
        policy = build_engineering_edit_policy(root)
    except (OSError, ValueError):
        return False
    if policy.get("status") != "ready":
        return False

    suffixes = policy.get("editable_suffixes")
    if not isinstance(suffixes, list) or posix.suffix.casefold() not in set(suffixes):
        return False

    source = root.joinpath(*posix.parts)
    if source.is_symlink() or not source.is_file():
        return False
    try:
        source.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def _bounded_strategy_rows(research: Mapping[str, object]) -> list[object]:
    raw = research.get("candidate_strategies")
    if not isinstance(raw, list):
        return []
    result: list[object] = []
    for row in raw[:MAX_RESEARCH_STRATEGIES]:
        if isinstance(row, str):
            text = _safe_text(row, MAX_RESEARCH_TEXT_CHARS)
            if text:
                result.append(text)
        elif isinstance(row, Mapping):
            clean = {}
            for key in ("strategy_id", "title", "summary", "rationale"):
                text = _safe_text(row.get(key), MAX_RESEARCH_TEXT_CHARS)
                if text:
                    clean[key] = text
            if clean:
                result.append(clean)
    return result


def _bounded_citation_rows(research: Mapping[str, object]) -> list[dict[str, object]]:
    raw = research.get("citations")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, object]] = []
    for row in raw[:MAX_RESEARCH_CITATIONS]:
        if not isinstance(row, Mapping):
            continue
        clean: dict[str, object] = {}
        for key in ("citation_id", "title", "provider", "year"):
            value = row.get(key)
            if key == "year":
                if type(value) is int:
                    clean[key] = value
                continue
            text = _safe_text(value, 300)
            if text:
                clean[key] = text
        excerpt = _safe_text(row.get("abstract_excerpt"), MAX_RESEARCH_TEXT_CHARS)
        if excerpt:
            clean["abstract_excerpt"] = excerpt
        if clean:
            result.append(clean)
    return result


def _language_hint(relative: str) -> str | None:
    suffix = PurePosixPath(relative).suffix.casefold()
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".dart": "dart",
        ".java": "java",
        ".c": "c",
        ".h": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cxx": "cpp",
        ".hh": "cpp",
        ".hpp": "cpp",
        ".rs": "rust",
        ".go": "go",
        ".sql": "sql",
    }.get(suffix)


def build_engineering_runtime_task(
    target: Mapping[str, object],
    evidence: Mapping[str, object],
    research: Mapping[str, object],
    *,
    task_id: str,
) -> dict[str, object]:
    relative = _target_path(target, evidence)
    if relative is None:
        raise ValueError("engineering runtime target path is invalid")

    opportunity = (
        evidence.get("opportunity")
        if isinstance(evidence.get("opportunity"), Mapping)
        else {}
    )
    summary = (
        _safe_text(opportunity.get("summary"), 1200)
        or _safe_text(target.get("summary"), 1200)
        or "Improve the selected engineering target while preserving behavior."
    )
    opportunity_type = _safe_text(opportunity.get("type"), 120)
    symbol = _safe_text(opportunity.get("symbol"), 240)

    instruction_parts = [
        f"Improve {relative} for the selected evidence-backed opportunity.",
        summary,
        "Preserve current observable behavior unless the success criteria explicitly require a bounded structural improvement.",
        "Make the smallest coherent source-only change. Do not edit dependency manifests, lockfiles, credentials, generated/vendor files, or protected SIRA paths.",
    ]
    if opportunity_type:
        instruction_parts.append(f"Opportunity type: {opportunity_type}.")
    if symbol:
        instruction_parts.append(f"Primary symbol: {symbol}.")

    strategies = _bounded_strategy_rows(research)
    if strategies:
        instruction_parts.append(
            "Research-backed candidate strategies (advisory, not authority): "
            + json.dumps(strategies, ensure_ascii=False, sort_keys=True)
        )

    instruction = " ".join(instruction_parts)
    if len(instruction) > MAX_RUNTIME_INSTRUCTION_CHARS:
        instruction = instruction[:MAX_RUNTIME_INSTRUCTION_CHARS]

    source_success = (
        dict(evidence["success_criteria"])
        if isinstance(evidence.get("success_criteria"), Mapping)
        else {}
    )
    success_criteria: dict[str, object] = {
        **source_success,
        "runtime_route": "protected_engineering_v1",
        "research_citations": _bounded_citation_rows(research),
        "research_id": research.get("research_id"),
        "opportunity_id": opportunity.get("opportunity_id") or target.get("opportunity_id"),
    }

    return {
        "task_id": task_id,
        "instruction": instruction,
        "target_paths": [relative],
        "language_hint": _language_hint(relative),
        "success_criteria": success_criteria,
        "diagnostics": [],
    }


def _external_state_root(root: Path) -> Path:
    root = Path(root).resolve()
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return root.parent / ".sira-engineering-runtime" / f"{root.name}-{digest}"


def _persist_handoff(root: Path, report: dict[str, object]) -> str:
    handoff_id = report.get("handoff_id")
    if not isinstance(handoff_id, str) or not handoff_id.startswith("ohf_"):
        raise ValueError("engineering runtime handoff id is invalid")
    artifact = (
        Path(root).resolve()
        / "improvements"
        / "opportunities"
        / "handoffs"
        / f"{handoff_id}.json"
    )
    report["artifact"] = str(artifact)
    write_json(artifact, report)
    return str(artifact)


def _finish(
    root: Path,
    opportunity: Mapping[str, object],
    report: dict[str, object],
) -> dict[str, object]:
    outcome = str(report.get("outcome") or report.get("status") or "engineering_runtime_unknown")[:120]
    try:
        cooldown = OpportunityStore(root).mark_attempt(dict(opportunity), outcome)
    except (OSError, ValueError):
        cooldown = None
    report["cooldown"] = cooldown
    _persist_handoff(root, report)
    return report


def _summary_mapping(value: object, keys: tuple[str, ...]) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {key: value.get(key) for key in keys}


def run_engineering_runtime_handoff(
    root: Path,
    target: Mapping[str, object],
    evidence: Mapping[str, object],
    research: Mapping[str, object],
    *,
    worker_task_id: str,
    runtime_guard: RuntimeGuard | None = None,
    writer_factory=make_default_engineering_writer,
    candidate_preparer=prepare_verified_engineering_candidate,
    evaluator=evaluate_engineering_candidate,
    authorizer=evaluate_engineering_authorization,
    promoter=promote_engineering_candidate,
    command_runner=None,
    bubblewrap_path: str | None = None,
    state_root: Path | None = None,
) -> dict[str, object]:
    """Run one bounded engineering handoff under existing protected gates."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("engineering runtime project root must exist")
    if not engineering_runtime_eligible(root, target, evidence, research):
        raise ValueError("opportunity is not eligible for engineering runtime")
    if not isinstance(worker_task_id, str) or not worker_task_id:
        raise ValueError("engineering runtime worker task id is required")

    opportunity = (
        dict(evidence["opportunity"])
        if isinstance(evidence.get("opportunity"), Mapping)
        else dict(target)
    )
    handoff_id = "ohf_" + uuid4().hex
    run_id = "er_" + uuid4().hex

    external = (
        Path(state_root).expanduser().absolute()
        if state_root is not None
        else _external_state_root(root)
    )
    try:
        external.resolve(strict=False).relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("engineering runtime state root must be outside main project")
    external.mkdir(parents=True, mode=0o700, exist_ok=True)

    run_root = external / "runs" / run_id
    run_root.mkdir(parents=True, mode=0o700, exist_ok=False)
    writer_workspace = run_root / "writer"
    transaction_root = run_root / "transaction"

    task = build_engineering_runtime_task(
        target,
        evidence,
        research,
        task_id=worker_task_id,
    )

    base: dict[str, object] = {
        "schema_version": 1,
        "kind": "opportunity_writer_handoff",
        "policy_version": ENGINEERING_RUNTIME_POLICY_VERSION,
        "runtime_route": "protected_engineering_v1",
        "handoff_id": handoff_id,
        "created_at": utc_now(),
        "opportunity_id": opportunity.get("opportunity_id"),
        "research_id": research.get("research_id"),
        "worker_task_id": worker_task_id,
        "status": "running",
        "outcome": None,
        "promotion_performed": False,
        "main_tree_modified": False,
        "writer_report": None,
        "engineering_writer_attempt": None,
        "engineering_evaluator": None,
        "engineering_authorization": None,
        "structural_check": None,
        "promotion": None,
        "runtime_guard_checked_before_promotion": False,
        "package_installation_performed": False,
        "main_tree_command_execution": False,
        "external_workspace": str(run_root),
        "artifact": None,
    }

    success = task.get("success_criteria")
    structural_goal = success.get("structural_goal") if isinstance(success, Mapping) else None
    structural_hypothesis = None
    if isinstance(success, Mapping) and "structural_goal" in success:
        symbol = opportunity.get("symbol")
        if isinstance(structural_goal, Mapping) and isinstance(symbol, str) and symbol:
            structural_hypothesis = {
                "target": {"path": _target_path(target, evidence), "symbol": symbol},
                "success_criteria": {"structural_goal": structural_goal},
            }
        try:
            baseline_check = (
                _structural_goal_check(root, root, structural_hypothesis)
                if structural_hypothesis is not None else None
            )
        except (OSError, UnicodeError, ValueError):
            baseline_check = None
        if not isinstance(baseline_check, Mapping):
            base["status"] = "rejected_structural_goal"
            base["outcome"] = "structural_goal_invalid"
            return _finish(root, opportunity, base)
        if baseline_check.get("baseline_matches") is not True:
            base["structural_check"] = baseline_check
            base["status"] = "rejected_structural_goal"
            base["outcome"] = "stale_structural_baseline"
            return _finish(root, opportunity, base)

    writer = writer_factory(root)
    attempt = candidate_preparer(
        root,
        task,
        writer_workspace,
        writer,
        command_runner=command_runner,
        bubblewrap_path=bubblewrap_path,
    )
    if not isinstance(attempt, Mapping):
        raise ValueError("engineering candidate preparer returned invalid data")
    attempt = dict(attempt)
    base["writer_report"] = (
        dict(attempt["writer_report"])
        if isinstance(attempt.get("writer_report"), Mapping)
        else None
    )
    base["engineering_writer_attempt"] = _summary_mapping(
        attempt,
        (
            "schema",
            "policy_version",
            "task_id",
            "candidate_root",
            "status",
            "verification_executed",
            "main_tree_modified",
            "package_installation_performed",
            "promotion_authorized",
        ),
    )

    if attempt.get("status") != "verified_candidate_ready":
        status = str(attempt.get("status") or "engineering_candidate_unavailable")
        base["status"] = status
        base["outcome"] = status
        return _finish(root, opportunity, base)

    if structural_hypothesis is not None:
        candidate_root = attempt.get("candidate_root")
        if not isinstance(candidate_root, str):
            check = None
        else:
            try:
                check = _structural_goal_check(
                    root,
                    Path(candidate_root),
                    structural_hypothesis,
                )
            except (OSError, UnicodeError, ValueError):
                check = None
        base["structural_check"] = check
        if not isinstance(check, Mapping) or check.get("decision") != "pass":
            base["status"] = "rejected_structural_goal"
            base["outcome"] = (
                str(check.get("decision_code") or "structural_goal_invalid")
                if isinstance(check, Mapping) else "structural_goal_invalid"
            )
            return _finish(root, opportunity, base)

    evaluator_report = evaluator(root, attempt)
    if not isinstance(evaluator_report, Mapping):
        raise ValueError("engineering evaluator returned invalid data")
    evaluator_report = dict(evaluator_report)
    base["engineering_evaluator"] = _summary_mapping(
        evaluator_report,
        (
            "schema",
            "policy_version",
            "decision",
            "decision_code",
            "recommendation",
            "promotion_candidate_eligible",
            "promotion_authorized",
            "main_tree_modified",
        ),
    )
    if evaluator_report.get("decision") != "accept":
        base["status"] = "rejected"
        base["outcome"] = str(
            evaluator_report.get("decision_code")
            or "engineering_evaluator_rejected"
        )
        return _finish(root, opportunity, base)

    authorization = authorizer(root, attempt, evaluator_report)
    if not isinstance(authorization, Mapping):
        raise ValueError("engineering authorization returned invalid data")
    authorization = dict(authorization)
    auth = authorization.get("authorization")
    base["engineering_authorization"] = {
        "decision": authorization.get("decision"),
        "decision_code": authorization.get("decision_code"),
        "promotion_allowed": authorization.get("promotion_allowed"),
        "authority_scope": authorization.get("authority_scope"),
        "fingerprint": (
            auth.get("fingerprint")
            if isinstance(auth, Mapping)
            else None
        ),
        "checksum_only": (
            auth.get("checksum_only")
            if isinstance(auth, Mapping)
            else None
        ),
    }
    if authorization.get("decision") != "allow":
        base["status"] = "denied"
        base["outcome"] = str(
            authorization.get("decision_code")
            or "engineering_authorization_denied"
        )
        return _finish(root, opportunity, base)

    base["runtime_guard_checked_before_promotion"] = True
    if runtime_guard is not None and not bool(runtime_guard()):
        base["status"] = "stopped_by_request"
        base["outcome"] = "stop_requested"
        return _finish(root, opportunity, base)

    promotion = promoter(
        root,
        attempt,
        evaluator_report,
        authorization,
        transaction_root,
        command_runner=command_runner,
        bubblewrap_path=bubblewrap_path,
    )
    if not isinstance(promotion, Mapping):
        raise ValueError("engineering promoter returned invalid data")
    promotion = dict(promotion)
    base["promotion"] = promotion

    promotion_status = str(promotion.get("status") or "engineering_promotion_failed")
    decision_code = str(
        promotion.get("decision_code")
        or promotion_status
    )
    if (
        promotion_status == "promoted"
        and decision_code == "engineering_promotion_committed"
        and promotion.get("promotion_performed") is True
    ):
        base["status"] = "promoted"
        base["outcome"] = "promotion_committed"
        base["promotion_performed"] = True
        base["main_tree_modified"] = True
    elif promotion_status == "rollback_failed":
        base["status"] = "rollback_failed"
        base["outcome"] = decision_code
        base["promotion_performed"] = False
        base["main_tree_modified"] = True
    else:
        base["status"] = promotion_status
        base["outcome"] = decision_code
        base["promotion_performed"] = False
        base["main_tree_modified"] = False

    return _finish(root, opportunity, base)
