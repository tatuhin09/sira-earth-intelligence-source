"""Bounded, evidence-led study selection. No source or skill authority is granted here."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Mapping
from uuid import uuid4

from .learning_goal_evidence_progress import verified_study_step_evidence
from .learning_goal_progress import study_step_statistics
from .learning_goal_followup import search_query_for_focus
from .memory_first import resolve_memory_first
from .models import utc_now
from .storage import write_json


MAX_EVENTS = 32
MAX_BYTES = 64_000
EVIDENCE_BACKOFF = 6 * 3600
PROVIDER_BACKOFF = 30 * 60
_GOAL_ID = re.compile(r"lg_[0-9a-f]{32}\Z")
_FAILURE = {"research_failed", "research_sources_insufficient",
            "independent_source_unavailable", "sources_discovered_needs_verification",
            "ambiguous_evidence", "stale_evidence", "contradictory_evidence"}


def _path(root: Path, goal_id: str) -> Path:
    if not isinstance(goal_id, str) or not _GOAL_ID.fullmatch(goal_id):
        raise ValueError("Invalid goal identifier")
    return Path(root).resolve() / "memory" / "learning_goal_adaptive" / (goal_id + ".json")


def _read(root: Path, goal_id: str) -> list[dict]:
    path = _path(root, goal_id)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Adaptive history is a symlink")
    try:
        if not 0 < path.stat().st_size <= MAX_BYTES:
            raise ValueError("Adaptive history exceeds bound")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Adaptive history is unreadable") from exc
    if (not isinstance(payload, dict) or payload.get("schema") != "sira.adaptive_study.v1"
            or payload.get("goal_id") != goal_id or not isinstance(payload.get("events"), list)
            or len(payload["events"]) > MAX_EVENTS):
        raise ValueError("Adaptive history is invalid")
    for row in payload["events"]:
        if (not isinstance(row, dict) or not isinstance(row.get("fingerprint"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["fingerprint"])
                or type(row.get("at_epoch")) not in (float, int)
                or not math.isfinite(row["at_epoch"]) or row["at_epoch"] < 0
                or row.get("classification") not in {"provider_failure", "evidence_gap", "verified", "other"}
                or not isinstance(row.get("decision_id"), str)
                or not isinstance(row.get("outcome"), str)):
            raise ValueError("Adaptive event is invalid")
    return payload["events"]


def _fingerprint(goal_id: str, focus: str, strategy: str) -> str:
    return hashlib.sha256(json.dumps([goal_id, focus, strategy], ensure_ascii=False,
                                   separators=(",", ":")).encode("utf-8")).hexdigest()


def _recent_report(root: Path, goal: Mapping) -> dict:
    path = Path(goal.get("last_report") or "")
    expected = Path(root).resolve() / "memory" / "learning_goal_reports"
    try:
        if (path.parent != expected or path.is_symlink() or not path.is_file()
                or path.stat().st_size > 2_000_000):
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) and value.get("learning_goal_id") == goal.get("goal_id") else {}


def classify_outcome(outcome: str, report: Mapping | None = None) -> str:
    """Provider failures get a shorter retry window than evidence failures."""
    report = report or {}
    research = report.get("research") or {}
    access = (research.get("open_access") or {}) if isinstance(research, Mapping) else {}
    if not isinstance(access, Mapping):
        access = {}
    failures = access.get("provider_failures") or []
    if (outcome in {"research_failed", "deferred_provider_cooldown"}
            or access.get("status") == "deferred_provider_cooldown"
            or bool(failures)):
        return "provider_failure"
    if outcome == "verified_knowledge_recorded":
        return "verified"
    return "evidence_gap" if outcome in _FAILURE else "other"


def preview_next_question(root: Path, goal: Mapping, *, now_epoch: float | None = None,
                          evidence: Mapping[str, list[str]] | None = None) -> dict | None:
    """Select one plan focus; keep all unverified and contradiction status explicit."""
    plan = goal.get("study_plan") or []
    when = time.time() if now_epoch is None else float(now_epoch)
    if not math.isfinite(when) or when < 0:
        raise ValueError("Invalid progression time")
    events = _read(root, goal["goal_id"])
    if not plan:
        fingerprint = _fingerprint(goal["goal_id"], goal["topic"], "owner_focus")
        prior = next((event for event in reversed(events) if event["fingerprint"] == fingerprint), None)
        if prior and prior["classification"] in {"provider_failure", "evidence_gap"}:
            duration = PROVIDER_BACKOFF if prior["classification"] == "provider_failure" else EVIDENCE_BACKOFF
            if when - prior["at_epoch"] < duration:
                return None
        return {"study_step_index": None, "focus": goal["topic"],
                "question": goal["topic"], "reason": "owner_topic",
                "strategy": "owner_focus",
                "fingerprint": fingerprint,
                "suppressed": [], "alternatives": []}
    covered = (verified_study_step_evidence(root, goal) if evidence is None else evidence)
    stats = study_step_statistics(goal)
    report = _recent_report(root, goal)
    considered = []
    for index, focus in enumerate(plan):
        claims = covered.get(focus) or []
        memory_reason = None
        # A claim may have been verified through another goal or the A77
        # general path; reuse it only if the strict A79 decision accepts it.
        if not claims and (Path(root).resolve() / "memory/sira_knowledge.sqlite3").is_file():
            memory = resolve_memory_first(root, focus)
            if memory["status"] == "memory_resolved":
                claims = [row["claim"] for row in memory["results"]]
            else:
                memory_reason = memory["reason"]
        attempt = stats[index]
        last_report = report if report.get("research_query") == focus else {}
        status = (last_report.get("document_review") or {}).get("status")
        last_outcome = last_report.get("outcome") or attempt.get("last_outcome")
        if memory_reason in {"contradicted_memory", "contradictory_verified_claims"}:
            reason, strategy, question, rank = ("resolve_contradiction", "contradiction_review",
                focus + " conflicting source evidence", 0)
        elif memory_reason == "stale_memory_requires_revalidation":
            reason, strategy, question, rank = ("revalidate_stale_knowledge", "fresh_independent_sources",
                focus + " updated independent evidence", 0)
        elif status == "contradictory_evidence" or last_outcome == "contradictory_evidence":
            reason, strategy, question, rank = ("resolve_contradiction", "contradiction_review",
                focus + " conflicting source evidence", 0)
        elif status in {"ambiguous_evidence", "insufficient_independent_sources",
                        "unverified_source_statements"} and not claims:
            reason, strategy, question, rank = ("strengthen_weak_evidence", "independent_corroboration",
                focus + " independent corroboration", 1)
        elif claims:
            reason, strategy, question, rank = "already_supported", "owner_focus", focus, 4
        elif attempt["attempt_count"] == 0:
            reason, strategy, question, rank = "unresolved_new_gap", "owner_focus", focus, 2
        else:
            reason, strategy, question, rank = ("unverified_gap", "alternate_source",
                focus + " independent primary source", 3)
        queued_query, queued_strategy = search_query_for_focus(goal, focus)
        if (not claims and queued_strategy == "unverified_title_followup"
                and all(stat["attempt_count"] for stat in stats)):
            reason, strategy, question, rank = ("queued_evidence_gap",
                "queued_" + queued_query[len(focus) + 1:], queued_query, 1)
        fingerprint = _fingerprint(goal["goal_id"], focus, strategy)
        prior = next((event for event in reversed(events) if event["fingerprint"] == fingerprint), None)
        suppression = None
        if prior and prior["classification"] in {"provider_failure", "evidence_gap"}:
            duration = PROVIDER_BACKOFF if prior["classification"] == "provider_failure" else EVIDENCE_BACKOFF
            if when - prior["at_epoch"] < duration:
                suppression = prior["classification"] + "_backoff"
        considered.append({"study_step_index": index, "focus": focus, "question": question[:200],
                           "reason": reason, "strategy": strategy, "fingerprint": fingerprint,
                           "suppression": suppression, "rank": rank,
                           "verified_claim_count": len(claims),
                           "attempt_count": attempt["attempt_count"]})
    available = [row for row in considered if row["suppression"] is None
                 and row["reason"] != "already_supported"]
    if not available:
        return None
    chosen = min(available, key=lambda row: (row["rank"], row["attempt_count"], row["study_step_index"]))
    return {key: chosen[key] for key in ("study_step_index", "focus", "question", "reason",
                                        "strategy", "fingerprint", "verified_claim_count") } | {
        "suppressed": [{"focus": row["focus"], "reason": row["suppression"]}
                       for row in considered if row["suppression"]][:12],
        "alternatives": [{"focus": row["focus"], "reason": row["reason"]}
                         for row in available if row is not chosen][:11],
    }


def record_progression(root: Path, goal_id: str, decision: Mapping, *, outcome: str,
                       report: Mapping | None = None, at_epoch: float | None = None) -> dict:
    """Persist only bounded decision provenance, outcome and suppression fingerprint."""
    when = time.time() if at_epoch is None else float(at_epoch)
    if not math.isfinite(when) or when < 0 or not isinstance(outcome, str) or len(outcome) > 120:
        raise ValueError("Invalid progression outcome")
    focus = decision.get("focus")
    strategy = decision.get("strategy")
    if (not isinstance(focus, str) or not isinstance(strategy, str)
            or not 1 <= len(focus) <= 500 or not 1 <= len(strategy) <= 100
            or not isinstance(decision.get("question"), str)
            or not 1 <= len(decision["question"]) <= 500
            or not isinstance(decision.get("reason"), str)
            or not 1 <= len(decision["reason"]) <= 120
            or decision.get("fingerprint") != _fingerprint(goal_id, focus, strategy)):
        raise ValueError("Invalid progression provenance")
    path = _path(root, goal_id)
    events = _read(root, goal_id)
    event = {"decision_id": "ad_" + uuid4().hex, "fingerprint": decision["fingerprint"],
             "focus": focus, "question": decision["question"], "strategy": strategy,
             "reason": decision["reason"], "study_step_index": decision["study_step_index"],
             "suppressed": decision.get("suppressed", [])[:12],
             "outcome": outcome, "classification": classify_outcome(outcome, report),
             "report_artifact": (report or {}).get("artifact"), "at_epoch": when}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(path, {"schema": "sira.adaptive_study.v1", "goal_id": goal_id,
                      "updated_at": utc_now(), "events": (events + [event])[-MAX_EVENTS:]})
    return event
