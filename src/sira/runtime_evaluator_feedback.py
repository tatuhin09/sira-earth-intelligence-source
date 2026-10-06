"""Bounded runtime evaluator feedback for Auto Benchmark / Evaluator v1.5C.

This module closes the runtime audit loop without creating a new authority
surface. Completed autonomous cycles emit a sanitized feedback artifact keyed
to a stable target identity. Later target selections may reuse the aggregate
history as context, but the history never changes ranking, authorizes promotion,
requests access, or weakens protected evaluators.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping
from uuid import uuid4

from .models import utc_now
from .storage import write_json

RUNTIME_FEEDBACK_POLICY_VERSION = 1
MAX_FEEDBACK_RECORDS = 500
MAX_FEEDBACK_SCAN = 200
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_HANDOFF_ID_RE = re.compile(r"ohf_[0-9a-f]{32}\Z")


def _bounded_text(value: object, limit: int = 120) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split()).strip()
    return value[:limit] if value else None


def stable_runtime_target_key(target: Mapping[str, object]) -> str:
    if not isinstance(target, Mapping):
        raise ValueError("runtime feedback target must be a mapping")
    kind = _bounded_text(target.get("target_kind"))
    if kind is None:
        if _bounded_text(target.get("memory_id")):
            kind = "memory"
        elif (
            _bounded_text(target.get("type"))
            and _bounded_text(target.get("path"), 500)
        ):
            kind = "opportunity"
        else:
            kind = "unknown"

    if kind == "memory":
        memory_id = _bounded_text(target.get("memory_id"))
        if not memory_id:
            raise ValueError("memory runtime feedback target requires memory_id")
        material = {"kind": "memory", "memory_id": memory_id}
    elif kind == "opportunity":
        path = _bounded_text(target.get("path"), 500)
        opportunity_type = _bounded_text(target.get("type"))
        symbol = _bounded_text(target.get("symbol"), 300)
        if not path or not opportunity_type:
            raise ValueError("opportunity runtime feedback target requires type/path")
        material = {
            "kind": "opportunity",
            "type": opportunity_type,
            "path": path,
            "symbol": symbol,
        }
    else:
        material = {
            "kind": kind,
            "memory_id": _bounded_text(target.get("memory_id")),
            "opportunity_id": _bounded_text(target.get("opportunity_id")),
            "path": _bounded_text(target.get("path"), 500),
            "capability": _bounded_text(target.get("capability")),
        }
        if not any(value for key, value in material.items() if key != "kind"):
            raise ValueError("runtime feedback target identity is incomplete")
    raw = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _target_summary(target: Mapping[str, object]) -> dict[str, object]:
    return {
        "target_kind": _bounded_text(target.get("target_kind")),
        "memory_id": _bounded_text(target.get("memory_id")),
        "opportunity_id": _bounded_text(target.get("opportunity_id")),
        "type": _bounded_text(target.get("type")),
        "path": _bounded_text(target.get("path"), 500),
        "symbol": _bounded_text(target.get("symbol"), 300),
        "capability": _bounded_text(target.get("capability")),
        "category": _bounded_text(target.get("category")),
    }


def _benchmark_snapshot(target: Mapping[str, object]) -> dict[str, object] | None:
    evidence = target.get("benchmark_evidence")
    if not isinstance(evidence, Mapping):
        return None
    history = evidence.get("history")
    history_summary = None
    if isinstance(history, Mapping):
        latest = history.get("latest_current")
        latest_summary = None
        if isinstance(latest, Mapping):
            latest_summary = {
                "suite": _bounded_text(latest.get("suite")),
                "suite_id": _bounded_text(latest.get("suite_id")),
                "passed": latest.get("passed") if type(latest.get("passed")) is int else None,
                "failed": latest.get("failed") if type(latest.get("failed")) is int else None,
                "current_code_match": latest.get("current_code_match") is True,
            }
        history_summary = {
            "current_code_reports": int(history.get("current_code_reports") or 0),
            "current_code_failed_reports": int(history.get("current_code_failed_reports") or 0),
            "current_code_passed_reports": int(history.get("current_code_passed_reports") or 0),
            "stale_or_unknown_reports": int(history.get("stale_or_unknown_reports") or 0),
            "recurring_weakness": history.get("recurring_weakness") is True,
            "latest_current": latest_summary,
        }
    boost = evidence.get("priority_boost")
    return {
        "suite": _bounded_text(evidence.get("suite")),
        "classification": _bounded_text(evidence.get("classification")),
        "priority_boost": boost if type(boost) is int and boost >= 0 else 0,
        "history": history_summary,
        "authority_granted": False,
        "promotion_authorized": False,
    }


def _cycle_summary(cycle: Mapping[str, object]) -> dict[str, object]:
    resource = cycle.get("resource_usage")
    return {
        "cycle_id": _bounded_text(cycle.get("cycle_id")),
        "target_selection_id": _bounded_text(cycle.get("target_selection_id")),
        "status": _bounded_text(cycle.get("status")) or "unknown",
        "outcome": _bounded_text(cycle.get("outcome")) or "unknown",
        "promotion_performed": cycle.get("promotion_performed") is True,
        "main_tree_modified": cycle.get("main_tree_modified") is True,
        "memory_id": _bounded_text(cycle.get("memory_id")),
        "opportunity_id": _bounded_text(cycle.get("opportunity_id")),
        "handoff_id": _bounded_text(cycle.get("handoff_id")),
        "promotion_id": _bounded_text(cycle.get("promotion_id")),
        "resource_usage": {
            "free_public_api_requests": (
                int(resource.get("free_public_api_requests") or 0)
                if isinstance(resource, Mapping)
                else 0
            ),
            "metered_model_requests": (
                int(resource.get("metered_model_requests") or 0)
                if isinstance(resource, Mapping)
                else 0
            ),
        },
    }


def _handoff_summary(root: Path, handoff_id: str | None) -> dict[str, object] | None:
    if not handoff_id or not _HANDOFF_ID_RE.fullmatch(handoff_id):
        return None
    path = root / "improvements" / "opportunities" / "handoffs" / f"{handoff_id}.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    evaluator2 = value.get("evaluator2")
    evolution = value.get("evolution_assessment")
    gap = value.get("capability_gap")
    return {
        "evaluator2": {
            "decision": _bounded_text(evaluator2.get("decision")),
            "decision_code": _bounded_text(evaluator2.get("decision_code")),
            "evaluation_classification": _bounded_text(
                evaluator2.get("evaluation_classification")
            ),
            "promotion_recommended": evaluator2.get("promotion_recommended") is True,
        } if isinstance(evaluator2, Mapping) else None,
        "evolution": {
            "classification": _bounded_text(evolution.get("classification")),
            "strategy_consumed": evolution.get("strategy_consumed") is True,
            "measured_comparison_available": (
                evolution.get("measured_comparison_available") is True
            ),
        } if isinstance(evolution, Mapping) else None,
        "capability_gap": {
            "gap_kind": _bounded_text(gap.get("gap_kind")),
            "authority_granted": False,
        } if isinstance(gap, Mapping) else None,
    }


def _feedback_classification(cycle: Mapping[str, object], handoff: Mapping[str, object] | None) -> str:
    if cycle.get("promotion_performed") is True or cycle.get("outcome") == "promotion_committed":
        return "promotion_committed"
    status = str(cycle.get("status") or "")
    outcome = str(cycle.get("outcome") or "")
    if status == "failed":
        return "cycle_failed"
    if status == "stopped_by_request":
        return "stopped"
    if status == "deferred_resource_budget":
        return "deferred"
    no_gain = {
        "structural_goal_not_met",
        "strategy_exhausted",
        "candidate_regression",
        "verification_shrinkage",
        "no_effect_change",
        "no_edit_proposed",
        "rejected_evaluator2",
    }
    if outcome in no_gain:
        return "measured_no_gain_or_rejection"
    if isinstance(handoff, Mapping):
        evolution = handoff.get("evolution")
        if isinstance(evolution, Mapping) and evolution.get("classification") in {
            "measured_no_gain", "candidate_rejected"
        }:
            return "measured_no_gain_or_rejection"
    return "completed_nonpromotion"


class RuntimeEvaluatorFeedbackStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.base = self.root / "improvements" / "evaluator" / "runtime_feedback"

    def _iter_records(self):
        if not self.base.is_dir() or self.base.is_symlink():
            return
        paths = []
        for path in self.base.glob("rf_*.json"):
            try:
                stamp = path.stat().st_mtime_ns
            except OSError:
                continue
            paths.append((stamp, path))
        for _, path in sorted(paths, reverse=True)[:MAX_FEEDBACK_SCAN]:
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(value, dict)
                and value.get("schema") == "sira.runtime_evaluator_feedback.v1"
                and isinstance(value.get("target_key"), str)
            ):
                yield value

    def record(
        self,
        cycle: Mapping[str, object],
        target: Mapping[str, object],
    ) -> dict[str, object]:
        target_key = stable_runtime_target_key(target)
        cycle_summary = _cycle_summary(cycle)
        handoff = _handoff_summary(self.root, cycle_summary.get("handoff_id"))
        feedback_id = "rf_" + uuid4().hex
        report = {
            "schema": "sira.runtime_evaluator_feedback.v1",
            "policy_version": RUNTIME_FEEDBACK_POLICY_VERSION,
            "feedback_id": feedback_id,
            "created_at": utc_now(),
            "target_key": target_key,
            "target": _target_summary(target),
            "benchmark_evidence": _benchmark_snapshot(target),
            "cycle": cycle_summary,
            "handoff_evidence": handoff,
            "classification": _feedback_classification(cycle, handoff),
            "raw_output_included": False,
            "used_for_ranking": False,
            "authority_granted": False,
            "promotion_authorized": False,
            "external_access_requested": False,
        }
        self.base.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = self.base / f"{feedback_id}.json"
        report["artifact"] = str(path)
        write_json(path, report)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return report

    def summary(self, target: Mapping[str, object]) -> dict[str, object]:
        target_key = stable_runtime_target_key(target)
        rows = [
            row for row in (self._iter_records() or ())
            if row.get("target_key") == target_key
        ]
        classifications: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        promoted = failed = 0
        for row in rows:
            classification = _bounded_text(row.get("classification")) or "unknown"
            classifications[classification] = classifications.get(classification, 0) + 1
            cycle = row.get("cycle")
            if isinstance(cycle, Mapping):
                outcome = _bounded_text(cycle.get("outcome")) or "unknown"
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                promoted += int(cycle.get("promotion_performed") is True)
                failed += int(cycle.get("status") == "failed")
        latest = rows[0] if rows else None
        return {
            "schema": "sira.runtime_evaluator_feedback_summary.v1",
            "policy_version": RUNTIME_FEEDBACK_POLICY_VERSION,
            "target_key": target_key,
            "cycle_count": len(rows),
            "promotion_count": promoted,
            "failed_cycle_count": failed,
            "classifications": dict(sorted(classifications.items())),
            "outcomes": dict(sorted(outcomes.items())),
            "latest": {
                "feedback_id": latest.get("feedback_id"),
                "created_at": latest.get("created_at"),
                "classification": latest.get("classification"),
                "outcome": (
                    latest.get("cycle", {}).get("outcome")
                    if isinstance(latest.get("cycle"), Mapping)
                    else None
                ),
            } if latest is not None else None,
            "history_applied": bool(rows),
            "used_for_ranking": False,
            "authority_granted": False,
            "promotion_authorized": False,
            "external_access_requested": False,
        }


def runtime_evaluator_feedback_summary(
    root: Path,
    target: Mapping[str, object],
) -> dict[str, object]:
    try:
        return RuntimeEvaluatorFeedbackStore(root).summary(target)
    except Exception as exc:
        return {
            "schema": "sira.runtime_evaluator_feedback_summary.v1",
            "policy_version": RUNTIME_FEEDBACK_POLICY_VERSION,
            "target_key": None,
            "cycle_count": 0,
            "history_applied": False,
            "status": "feedback_unavailable",
            "error_type": type(exc).__name__,
            "used_for_ranking": False,
            "authority_granted": False,
            "promotion_authorized": False,
            "external_access_requested": False,
        }


def safe_record_runtime_evaluator_feedback(
    root: Path,
    cycle: Mapping[str, object],
    target: Mapping[str, object],
) -> dict[str, object]:
    try:
        return RuntimeEvaluatorFeedbackStore(root).record(cycle, target)
    except Exception as exc:
        return {
            "schema": "sira.runtime_evaluator_feedback.v1",
            "policy_version": RUNTIME_FEEDBACK_POLICY_VERSION,
            "status": "feedback_unavailable",
            "error_type": type(exc).__name__,
            "raw_output_included": False,
            "used_for_ranking": False,
            "authority_granted": False,
            "promotion_authorized": False,
            "external_access_requested": False,
        }
