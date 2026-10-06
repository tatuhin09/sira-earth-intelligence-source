"""Measured Evolution v1.4 outcome assessment and capability-gap audit.

This module is deliberately non-authoritative. It summarizes already-produced
candidate evidence, decides whether an attempted strategy generated meaningful
evidence, and records bounded strategy-exhaustion gaps. It never edits source,
requests external authority, evaluates protected policy, or promotes code.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .autonomous_targeting import opportunity_lineage_key
from .models import utc_now
from .storage import write_json

ASSESSMENT_POLICY_VERSION = 1
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


class EvolutionAssessmentError(ValueError):
    pass


def _safe_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _test_summary(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    tests = value.get("tests")
    benchmark = value.get("benchmark")
    result: dict[str, object] = {
        "overall_passed": _safe_bool(value.get("overall_passed")),
        "tests_passed": None,
        "test_count": None,
        "benchmark_passed": None,
        "benchmark_passed_cases": None,
        "benchmark_failed_cases": None,
    }
    if isinstance(tests, Mapping):
        result["tests_passed"] = _safe_bool(tests.get("passed"))
        count = tests.get("test_count")
        result["test_count"] = count if type(count) is int and count >= 0 else None
    if isinstance(benchmark, Mapping):
        result["benchmark_passed"] = _safe_bool(benchmark.get("passed"))
        passed = benchmark.get("passed_cases")
        failed = benchmark.get("failed_cases")
        result["benchmark_passed_cases"] = passed if type(passed) is int and passed >= 0 else None
        result["benchmark_failed_cases"] = failed if type(failed) is int and failed >= 0 else None
    return result


def _structural_summary(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    keep = {}
    for key in (
        "metric", "path", "symbol", "expected_baseline", "baseline", "candidate",
        "baseline_matches", "decision", "decision_code", "target_max", "target_min",
    ):
        item = value.get(key)
        if item is None or isinstance(item, (str, int, bool)):
            keep[key] = item
    return keep


def assess_evolution_attempt(attempt: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(attempt, Mapping):
        raise EvolutionAssessmentError("attempt must be a mapping")

    status = str(attempt.get("status") or "unknown")[:120]
    outcome = str(attempt.get("outcome") or status)[:120]
    promoted = bool(attempt.get("promotion_performed"))
    baseline = _test_summary(attempt.get("baseline"))
    candidate = _test_summary(attempt.get("candidate"))
    structural = _structural_summary(attempt.get("structural_check"))

    if promoted or outcome == "promotion_committed":
        classification = "promotion_gain"
        consumed = True
        measured = True
    elif status in {"writer_error"}:
        classification = "infrastructure_neutral"
        consumed = False
        measured = False
    elif status in {"strategy_exhausted", "recent_success_suppressed"}:
        classification = "not_attempted"
        consumed = False
        measured = False
    elif status in {"rejected_structural_goal", "no_effect_change", "no_edit_proposed"}:
        classification = "measured_no_gain"
        consumed = True
        measured = structural is not None or status != "rejected_structural_goal"
    elif status in {"rejected", "denied"}:
        classification = "candidate_rejected"
        consumed = True
        measured = baseline is not None or candidate is not None
    else:
        classification = "attempted_other"
        consumed = True
        measured = baseline is not None or candidate is not None or structural is not None

    return {
        "schema": "sira.evolution_attempt_assessment.v1",
        "policy_version": ASSESSMENT_POLICY_VERSION,
        "classification": classification,
        "strategy_consumed": consumed,
        "measured_comparison_available": measured,
        "promotion_performed": promoted,
        "status": status,
        "outcome": outcome,
        "structural": structural,
        "baseline": baseline,
        "candidate": candidate,
        "raw_output_included": False,
        "authority_granted": False,
    }


class EvolutionGapStore:
    """Persist one bounded current exhaustion gap per stable opportunity lineage."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.base = self.root / "improvements" / "evolution" / "gaps"

    def record_strategy_exhaustion(
        self,
        opportunity: Mapping[str, object],
        evolution_plan: Mapping[str, object],
    ) -> dict[str, object]:
        if not isinstance(opportunity, Mapping) or not isinstance(evolution_plan, Mapping):
            raise EvolutionAssessmentError("gap inputs must be mappings")
        lineage_key = opportunity_lineage_key(dict(opportunity))
        if evolution_plan.get("status") != "strategy_exhausted":
            raise EvolutionAssessmentError("only strategy exhaustion can create an evolution gap")

        attempted = evolution_plan.get("attempted_strategy_count")
        candidates = evolution_plan.get("candidate_strategy_count")
        attempted = attempted if type(attempted) is int and attempted >= 0 else 0
        candidates = candidates if type(candidates) is int and candidates >= 0 else 0
        gap = {
            "schema": "sira.evolution_capability_gap.v1",
            "policy_version": ASSESSMENT_POLICY_VERSION,
            "created_at": utc_now(),
            "lineage_key": lineage_key,
            "gap_kind": "strategy_exhausted",
            "target": {
                "type": opportunity.get("type"),
                "path": opportunity.get("path"),
                "symbol": opportunity.get("symbol"),
            },
            "attempted_strategy_count": attempted,
            "candidate_strategy_count": candidates,
            "next_action": "collect_new_evidence_or_generate_distinct_strategy",
            "authority_granted": False,
            "external_access_requested": False,
            "promotion_authorized": False,
        }
        path = self.base / f"{lineage_key}.json"
        write_json(path, gap)
        gap["artifact"] = str(path)
        write_json(path, gap)
        return gap

    def load(self, lineage_key: str) -> dict[str, object] | None:
        if not isinstance(lineage_key, str) or len(lineage_key) != 64:
            raise EvolutionAssessmentError("invalid lineage key")
        path = self.base / f"{lineage_key}.json"
        try:
            if path.is_symlink() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise EvolutionAssessmentError("gap artifact is unsafe")
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvolutionAssessmentError("gap artifact is corrupt") from exc
        if not isinstance(data, dict) or data.get("schema") != "sira.evolution_capability_gap.v1":
            raise EvolutionAssessmentError("gap artifact format is invalid")
        return data
