"""Runtime skill consolidation from protected successful promotion outcomes.

This module learns only from already-completed protected promotion results.
Learned skill context is advisory and grants no promotion/access/spending authority.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .learning_consolidation import (
    LearningConsolidationError,
    LearningConsolidationStore,
)

_SKILL_SPECS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "complex_function": (
        "code.refactor.complex_function",
        "Refactor a complex function safely",
        (
            "Bind the task to the measured local structural goal and current source version.",
            "Use research evidence and existing tests to choose the smallest behavior-preserving extraction.",
            "Apply edits only in the isolated candidate workspace.",
            "Require targeted tests, mapped benchmark, Evaluator 2, Evaluator 1, and transactional promotion to pass.",
        ),
    ),
    "large_module": (
        "code.refactor.large_module",
        "Modularize a large module safely",
        (
            "Bind the task to the measured module target and current source version.",
            "Extract one cohesive responsibility behind stable interfaces.",
            "Apply edits only in the isolated candidate workspace.",
            "Require targeted tests, mapped benchmark, Evaluator 2, Evaluator 1, and transactional promotion to pass.",
        ),
    ),
    "explicit_debt_marker": (
        "code.repair.explicit_debt",
        "Resolve explicit technical debt safely",
        (
            "Bind the debt marker to current local evidence and a measurable success condition.",
            "Choose the smallest behavior-preserving repair supported by research and tests.",
            "Apply edits only in the isolated candidate workspace.",
            "Require targeted tests, mapped benchmark, Evaluator 2, Evaluator 1, and transactional promotion to pass.",
        ),
    ),
    "missing_test_reference": (
        "code.test.characterization",
        "Add focused characterization coverage",
        (
            "Identify the uncovered public behavior from current local evidence.",
            "Add focused observable assertions without coupling to implementation details.",
            "Keep production behavior unchanged unless the new test proves a real defect.",
            "Require mapped benchmark and both evaluators before any promotion.",
        ),
    ),
    "weak_error_handling": (
        "code.repair.error_handling",
        "Strengthen error handling safely",
        (
            "Identify the measured weak error-handling signal and current fallback contract.",
            "Narrow exception handling without hiding unexpected failures.",
            "Apply edits only in the isolated candidate workspace.",
            "Require targeted tests, mapped benchmark, Evaluator 2, Evaluator 1, and transactional promotion to pass.",
        ),
    ),
    "duplicate_function_body": (
        "code.refactor.duplicate_body",
        "Remove duplicated function behavior safely",
        (
            "Confirm the measured duplicate body and current public contracts.",
            "Use one canonical implementation or a small shared helper without unnecessary abstraction.",
            "Apply edits only in the isolated candidate workspace.",
            "Require targeted tests, mapped benchmark, Evaluator 2, Evaluator 1, and transactional promotion to pass.",
        ),
    ),
    "syntax_parse_failure": (
        "code.repair.syntax_parse",
        "Repair a syntax or parse failure safely",
        (
            "Bind the repair to the current parse failure and source version.",
            "Make the smallest syntax repair that restores the intended local contract.",
            "Apply edits only in the isolated candidate workspace.",
            "Require tests, mapped benchmark, Evaluator 2, Evaluator 1, and transactional promotion to pass.",
        ),
    ),
}


def skill_spec_for_opportunity(
    opportunity: Mapping[str, object],
) -> dict[str, object] | None:
    kind = opportunity.get("type")
    if not isinstance(kind, str):
        return None
    spec = _SKILL_SPECS.get(kind)
    if spec is None:
        return None
    key, title, steps = spec
    return {
        "skill_key": key,
        "title": title,
        "steps": list(steps),
        "opportunity_type": kind,
    }


def _verified_success(report: Mapping[str, object]) -> bool:
    structural = (
        report.get("structural_check")
        if isinstance(report.get("structural_check"), Mapping)
        else {}
    )
    return bool(
        report.get("status") == "promoted"
        and report.get("outcome") == "promotion_committed"
        and report.get("promotion_performed") is True
        and report.get("main_tree_modified") is True
        and structural.get("decision") == "pass"
    )


def record_verified_handoff_skill(
    root: Path,
    opportunity: Mapping[str, object],
    report: Mapping[str, object],
) -> dict[str, object]:
    spec = skill_spec_for_opportunity(opportunity)
    if spec is None:
        return {
            "status": "not_applicable",
            "reason": "unsupported_opportunity_type",
            "evidence_recorded": False,
            "consolidation": None,
            "authority_granted": False,
            "promotion_authorized": False,
        }
    if not _verified_success(report):
        return {
            "status": "not_recorded",
            "reason": "protected_success_not_proven",
            "skill_key": spec["skill_key"],
            "evidence_recorded": False,
            "consolidation": None,
            "authority_granted": False,
            "promotion_authorized": False,
        }

    handoff_id = report.get("handoff_id")
    if not isinstance(handoff_id, str):
        raise ValueError("verified handoff requires a handoff_id")

    store = LearningConsolidationStore(Path(root).resolve())
    inserted = store.record_skill_evidence(
        str(spec["skill_key"]),
        str(spec["title"]),
        tuple(str(step) for step in spec["steps"]),
        evidence_id=handoff_id,
        succeeded=True,
        confidence=1.0,
        source_kind="verified_protected_promotion",
    )
    decision = store.consolidate_skill(str(spec["skill_key"]))
    return {
        "status": "recorded" if inserted else "duplicate_evidence",
        "reason": "verified_protected_promotion",
        "skill_key": spec["skill_key"],
        "evidence_recorded": inserted,
        "consolidation": decision.to_dict(),
        "authority_granted": False,
        "promotion_authorized": False,
    }


def safe_record_verified_handoff_skill(
    root: Path,
    opportunity: Mapping[str, object],
    report: Mapping[str, object],
) -> dict[str, object]:
    try:
        return record_verified_handoff_skill(root, opportunity, report)
    except (LearningConsolidationError, OSError, ValueError, TypeError) as exc:
        return {
            "status": "learning_error",
            "reason": type(exc).__name__,
            "evidence_recorded": False,
            "consolidation": None,
            "authority_granted": False,
            "promotion_authorized": False,
        }


def skill_context_for_opportunity(
    root: Path,
    opportunity: Mapping[str, object],
) -> dict[str, object] | None:
    spec = skill_spec_for_opportunity(opportunity)
    if spec is None:
        return None
    try:
        skills = LearningConsolidationStore(Path(root).resolve()).list_skills()
    except (LearningConsolidationError, OSError):
        return None
    for skill in skills:
        if skill.get("skill_key") != spec["skill_key"]:
            continue
        return {
            "skill_key": skill["skill_key"],
            "title": skill["title"],
            "steps": list(skill["steps"]),
            "evidence_count": int(skill["evidence_count"]),
            "confidence": float(skill["confidence"]),
            "source": "consolidated_verified_promotion_history",
            "advisory_only": True,
            "authority_granted": False,
            "promotion_authorized": False,
        }
    return None


def attach_skill_context(
    root: Path,
    opportunity: Mapping[str, object],
    hypothesis: Mapping[str, object],
) -> dict[str, object]:
    """Add a matching consolidated skill without changing the hypothesis contract."""
    result = dict(hypothesis)
    context = (
        dict(result.get("opportunity_context"))
        if isinstance(result.get("opportunity_context"), Mapping)
        else {}
    )
    learned = skill_context_for_opportunity(Path(root).resolve(), opportunity)
    context["consolidated_skill"] = learned
    result["opportunity_context"] = context

    if learned is not None:
        raw_steps = learned.get("steps")
        steps = (
            [str(step) for step in raw_steps[:4] if isinstance(step, str)]
            if isinstance(raw_steps, list)
            else []
        )
        addition = (
            " Verified reusable skill from prior protected successful promotions: "
            + "; ".join(steps)
        )
        rationale = str(result.get("rationale") or "")
        result["rationale"] = (rationale + addition)[:4000]

    return result
