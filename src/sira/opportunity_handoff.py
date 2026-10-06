"""Research-ready opportunity handoff into the protected autonomous promotion chain."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping
from uuid import uuid4

from .autonomous_promotion import run_autonomous_candidate_promotion
from .code_writer import make_default_code_writer
from .evolution import StrategyEvolutionPlanner
from .evolution_assessment import EvolutionGapStore, assess_evolution_attempt
from .models import ProviderError, utc_now
from .opportunity import OpportunityStore
from .opportunity_research import _load_evidence, _safe_json, _validate_current_target
from .storage import write_json

HANDOFF_POLICY_VERSION = 2
_RESEARCH_ID_RE = re.compile(r"or_[0-9a-f]{32}\Z")


def _load_research(root: Path, research_id: str) -> dict[str, Any]:
    if not isinstance(research_id, str) or not _RESEARCH_ID_RE.fullmatch(research_id):
        raise ValueError("Invalid opportunity research ID")
    path = Path(root).resolve() / "improvements" / "opportunities" / "research" / f"{research_id}.json"
    report = _safe_json(path)
    if report is None or report.get("kind") != "opportunity_free_research_brief" or report.get("research_id") != research_id:
        raise ValueError("Opportunity research brief not found")
    return report


def _capability_for_suite(suite: object) -> str:
    mapping = {
        "paper-reading": "paper_reading",
        "papers": "scholarly_research",
        "reading": "reading",
        "retrieval": "retrieval",
        "synthesis": "synthesis",
        "memory": "memory",
        "improvement": "improvement",
    }
    return mapping.get(str(suite), "code_quality")


def _writer_hypothesis_core(
    research: Mapping[str, object],
    evidence: Mapping[str, object],
    *,
    evolution_plan: Mapping[str, object] | None = None,
) -> dict[str, object]:
    target = research.get("target") if isinstance(research.get("target"), Mapping) else {}
    path = target.get("path")
    symbol = target.get("symbol")
    suite = target.get("benchmark_suite")
    goal = target.get("structural_goal") if isinstance(target.get("structural_goal"), Mapping) else {}
    citations = research.get("citations") if isinstance(research.get("citations"), list) else []
    strategies = research.get("candidate_strategies") if isinstance(research.get("candidate_strategies"), list) else []
    selected_strategy = (
        evolution_plan.get("selected_strategy")
        if isinstance(evolution_plan, Mapping)
        and isinstance(evolution_plan.get("selected_strategy"), Mapping)
        else None
    )
    writer_strategies = [selected_strategy] if selected_strategy is not None else strategies[:3]
    opportunity = evidence.get("opportunity") if isinstance(evidence.get("opportunity"), Mapping) else {}
    research_id = research.get("research_id")
    opportunity_id = research.get("opportunity_id")
    if type(goal.get("target_max")) is int:
        goal_text = f"{goal.get('metric')} <= {goal.get('target_max')}"
    elif type(goal.get("target_min")) is int:
        goal_text = f"{goal.get('metric')} >= {goal.get('target_min')}"
    else:
        goal_text = str(goal.get("metric") or "measurable local goal")
    statement = (
        f"Improve {path}:{symbol} with the smallest behavior-preserving change that meets "
        f"the measured structural goal {goal_text}."
    )
    strategy_text = "; ".join(
        str(row.get("strategy")) for row in writer_strategies
        if isinstance(row, Mapping) and isinstance(row.get("strategy"), str)
    )
    rationale = (
        "Use the existing tests and mapped benchmark as behavioral oracles. "
        f"Research-supported strategies: {strategy_text}"
    )[:4000]
    return {
        "hypothesis_id": "oh_" + uuid4().hex,
        "memory_id": None,
        "benchmark_suite": suite,
        "statement": statement,
        "rationale": rationale,
        "memory_snapshot": {
            "kind": "opportunity",
            "category": "code_quality",
            "capability": _capability_for_suite(suite),
            "provider": "local_opportunity_discovery",
            "error_code": None,
            "summary": opportunity.get("summary"),
            "occurrence_count": 1,
            "status": "researched",
        },
        "related_memories": evidence.get("related_memories") if isinstance(evidence.get("related_memories"), list) else [],
        "target": {"path": path, "symbol": symbol},
        "success_criteria": {
            "structural_goal": dict(goal),
            "unit_tests_must_pass": True,
            "test_count_must_not_decrease": True,
            "relevant_benchmark_must_pass": True,
            "protected_shell_must_remain_unchanged": True,
        },
        "opportunity_context": {
            "opportunity_id": opportunity_id,
            "research_id": research_id,
            "citations": citations[:4],
            "candidate_strategies": writer_strategies[:1],
            "selected_strategy": dict(selected_strategy) if selected_strategy is not None else None,
            "selected_strategy_fingerprint": (
                evolution_plan.get("selected_strategy_fingerprint")
                if isinstance(evolution_plan, Mapping)
                else None
            ),
            "evolution_lineage_key": (
                evolution_plan.get("lineage_key")
                if isinstance(evolution_plan, Mapping)
                else None
            ),
        },
    }


def _strategy_outcome_is_evidence(attempt: Mapping[str, object]) -> bool:
    status = str(attempt.get("status") or "")
    outcome = str(attempt.get("outcome") or "")
    if status == "writer_error":
        return False
    if bool(attempt.get("promotion_performed")) or outcome == "promotion_committed":
        return True
    return status in {
        "no_edit_proposed",
        "rejected_structural_goal",
        "rejected",
        "no_effect_change",
        "denied",
        "promoted",
    }


def _write_handoff_report(
    root: Path,
    *,
    research_id: str,
    evidence_id: str,
    opportunity: Mapping[str, object],
    research: Mapping[str, object],
    status: str,
    outcome: str,
    attempt: Mapping[str, object],
    cooldown: Mapping[str, object] | None,
    evolution_plan: Mapping[str, object],
    evolution_outcome: Mapping[str, object] | None,
    evolution_assessment: Mapping[str, object],
    capability_gap: Mapping[str, object] | None,
) -> dict[str, object]:
    handoff_id = "ohf_" + uuid4().hex
    report: dict[str, object] = {
        "schema_version": 1,
        "kind": "opportunity_writer_handoff",
        "policy_version": HANDOFF_POLICY_VERSION,
        "handoff_id": handoff_id,
        "created_at": utc_now(),
        "research_id": research_id,
        "evidence_id": evidence_id,
        "opportunity_id": opportunity.get("opportunity_id"),
        "target": research.get("target"),
        "status": status,
        "outcome": outcome,
        "promotion_performed": bool(attempt.get("promotion_performed")),
        "main_tree_modified": bool(attempt.get("main_tree_modified")),
        "structural_check": attempt.get("structural_check"),
        "writer_report": attempt.get("writer_report"),
        "evaluator2": attempt.get("evaluator2"),
        "evaluator1": attempt.get("evaluator1"),
        "promotion": attempt.get("promotion"),
        "attempt": dict(attempt),
        "cooldown": dict(cooldown) if isinstance(cooldown, Mapping) else None,
        "evolution_plan": dict(evolution_plan),
        "evolution_outcome": (
            dict(evolution_outcome)
            if isinstance(evolution_outcome, Mapping)
            else None
        ),
        "evolution_assessment": dict(evolution_assessment),
        "capability_gap": (
            dict(capability_gap)
            if isinstance(capability_gap, Mapping)
            else None
        ),
    }
    artifact = root / "improvements" / "opportunities" / "handoffs" / f"{handoff_id}.json"
    report["artifact"] = str(artifact)
    write_json(artifact, report)
    return report


def _run_opportunity_writer_handoff_core(
    root: Path,
    research_id: str,
    *,
    writer=None,
    runner=None,
) -> dict[str, object]:
    root = Path(root).resolve()
    research = _load_research(root, research_id)
    quality = research.get("research_quality") if isinstance(research.get("research_quality"), Mapping) else {}
    if research.get("writer_handoff_allowed") is not True or quality.get("decision") != "writer_ready":
        raise ValueError("Opportunity research is not writer-ready")
    if research.get("paid_spending") is not False or research.get("metered_model_requests") != 0:
        raise ValueError("Opportunity research provenance is not free/public-only")

    evidence_id = research.get("evidence_id")
    evidence = _load_evidence(root, evidence_id)
    _validate_current_target(root, evidence)
    opportunity = evidence.get("opportunity") if isinstance(evidence.get("opportunity"), dict) else {}
    if research.get("opportunity_id") != opportunity.get("opportunity_id"):
        raise ValueError("Opportunity research does not match local evidence")

    strategies = (
        research.get("candidate_strategies")
        if isinstance(research.get("candidate_strategies"), list)
        else []
    )
    evolution = StrategyEvolutionPlanner(root)
    evolution_plan = evolution.plan(opportunity, strategies)
    evolution_status = str(evolution_plan.get("status") or "strategy_exhausted")
    if evolution_status != "selected":
        attempt = {
            "status": evolution_status,
            "outcome": evolution_status,
            "promotion_performed": False,
            "main_tree_modified": False,
            "writer_report": None,
            "structural_check": None,
            "baseline": None,
            "candidate": None,
            "evaluator2": None,
            "evaluator1": None,
            "promotion": None,
        }
        evolution_assessment = assess_evolution_attempt(attempt)
        cooldown = None
        capability_gap = None
        if evolution_status == "strategy_exhausted":
            cooldown = OpportunityStore(root).mark_attempt(
                opportunity, "strategy_exhausted"
            )
            capability_gap = EvolutionGapStore(root).record_strategy_exhaustion(
                opportunity, evolution_plan
            )
        return _write_handoff_report(
            root,
            research_id=research_id,
            evidence_id=evidence_id,
            opportunity=opportunity,
            research=research,
            status=evolution_status,
            outcome=evolution_status,
            attempt=attempt,
            cooldown=cooldown,
            evolution_plan=evolution_plan,
            evolution_outcome=None,
            evolution_assessment=evolution_assessment,
            capability_gap=capability_gap,
        )

    selected_strategy = evolution_plan.get("selected_strategy")
    if not isinstance(selected_strategy, Mapping):
        raise ValueError("Evolution planner selected an invalid strategy")

    hypothesis = _writer_hypothesis(
        research,
        evidence,
        evolution_plan=evolution_plan,
    )
    writer = writer or make_default_code_writer(root)
    try:
        attempt = run_autonomous_candidate_promotion(root, hypothesis, writer=writer, runner=runner)
    except ProviderError as exc:
        attempt = {
            "status": "writer_error",
            "outcome": exc.code,
            "promotion_performed": False,
            "main_tree_modified": False,
            "writer_report": getattr(writer, "last_report", None),
            "error": {"type": "ProviderError", "code": exc.code, "api_requests": exc.request_count},
        }
    except (ValueError, OSError, UnicodeError) as exc:
        attempt = {
            "status": "writer_error",
            "outcome": "writer_validation_error",
            "promotion_performed": False,
            "main_tree_modified": False,
            "writer_report": getattr(writer, "last_report", None),
            "error": {"type": type(exc).__name__, "code": "writer_validation_error"},
        }

    outcome = str(attempt.get("outcome") or attempt.get("status") or "unknown")[:120]
    cooldown = OpportunityStore(root).mark_attempt(opportunity, outcome)
    evolution_assessment = assess_evolution_attempt(attempt)
    evolution_outcome = None
    if evolution_assessment.get("strategy_consumed") is True:
        evolution_outcome = evolution.record_outcome(
            opportunity,
            selected_strategy,
            outcome=outcome,
            promotion_performed=bool(attempt.get("promotion_performed")),
        )

    return _write_handoff_report(
        root,
        research_id=research_id,
        evidence_id=evidence_id,
        opportunity=opportunity,
        research=research,
        status=str(attempt.get("status") or "unknown"),
        outcome=str(attempt.get("outcome") or attempt.get("status") or "unknown"),
        attempt=attempt,
        cooldown=cooldown,
        evolution_plan=evolution_plan,
        evolution_outcome=evolution_outcome,
        evolution_assessment=evolution_assessment,
        capability_gap=None,
    )

def _root_from_research_artifact(research: Mapping[str, object]) -> Path | None:
    artifact = research.get("artifact")
    if not isinstance(artifact, str) or not artifact:
        return None
    try:
        path = Path(artifact).resolve()
        if (
            path.parent.name != "research"
            or path.parent.parent.name != "opportunities"
            or path.parent.parent.parent.name != "improvements"
        ):
            return None
        return path.parents[3]
    except (OSError, RuntimeError, IndexError):
        return None


def _writer_hypothesis(*args, **kwargs):
    hypothesis = _writer_hypothesis_core(*args, **kwargs)
    research = args[0] if args and isinstance(args[0], Mapping) else kwargs.get("research")
    evidence = (
        args[1]
        if len(args) > 1 and isinstance(args[1], Mapping)
        else kwargs.get("evidence")
    )
    if not isinstance(research, Mapping) or not isinstance(evidence, Mapping):
        return hypothesis
    root = _root_from_research_artifact(research)
    opportunity = (
        evidence.get("opportunity")
        if isinstance(evidence.get("opportunity"), Mapping)
        else {}
    )
    if root is None:
        return hypothesis
    try:
        from .skill_runtime import attach_skill_context
        return attach_skill_context(root, opportunity, hypothesis)
    except (OSError, ValueError, TypeError):
        return hypothesis


def run_opportunity_writer_handoff(
    root: Path,
    research_id: str,
    *,
    writer=None,
    runner=None,
) -> dict[str, object]:
    root = Path(root).resolve()
    report = _run_opportunity_writer_handoff_core(
        root,
        research_id,
        writer=writer,
        runner=runner,
    )
    if not isinstance(report, Mapping):
        raise ValueError("Opportunity handoff returned invalid data")
    enriched = dict(report)

    try:
        research = _load_research(root, research_id)
        evidence_id = research.get("evidence_id")
        evidence = _load_evidence(root, evidence_id)
        opportunity = (
            evidence.get("opportunity")
            if isinstance(evidence.get("opportunity"), Mapping)
            else {}
        )
        from .skill_runtime import safe_record_verified_handoff_skill
        enriched["skill_learning"] = safe_record_verified_handoff_skill(
            root, opportunity, enriched
        )
    except (OSError, ValueError, TypeError) as exc:
        enriched["skill_learning"] = {
            "status": "learning_error",
            "reason": type(exc).__name__,
            "evidence_recorded": False,
            "consolidation": None,
            "authority_granted": False,
            "promotion_authorized": False,
        }

    artifact = enriched.get("artifact")
    if isinstance(artifact, str) and artifact:
        try:
            artifact_path = Path(artifact).resolve()
            handoff_dir = (
                root / "improvements" / "opportunities" / "handoffs"
            ).resolve()
            if (
                artifact_path.parent == handoff_dir
                and artifact_path.name.startswith("ohf_")
                and artifact_path.suffix == ".json"
            ):
                write_json(artifact_path, enriched)
        except (OSError, RuntimeError):
            pass

    return enriched
