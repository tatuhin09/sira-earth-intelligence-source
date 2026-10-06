"""Bounded study progress and gap-aware follow-up for arbitrary topics."""
from __future__ import annotations

from typing import Mapping


def study_step_statistics(goal: Mapping[str, object]) -> list[dict[str, object]]:
    """Read durable counters, or reconstruct them from an older goal's history."""
    plan = goal.get("study_plan") or []
    stored = goal.get("study_step_stats")
    if isinstance(stored, list) and len(stored) == len(plan):
        return [dict(row) for row in stored]
    stats: list[dict[str, object]] = [
        {"attempt_count": 0, "verified_claim_events": 0,
         "last_attempt_at_epoch": None, "last_outcome": None}
        for _ in plan
    ]
    for entry in goal.get("study_history") or []:
        index = entry["index"]
        row = stats[index]
        row["attempt_count"] += 1
        row["verified_claim_events"] += entry["verified_claim_count"]
        row["last_attempt_at_epoch"] = entry["attempted_at_epoch"]
        row["last_outcome"] = entry["outcome"]
    return stats


def next_study_step(stats: list[Mapping[str, object]], history: list[dict]) -> int:
    """Cover every step, then revisit evidence gaps without starving the rest."""
    unattempted = [i for i, row in enumerate(stats) if row["attempt_count"] == 0]
    if unattempted:
        return unattempted[0]
    gaps = [i for i, row in enumerate(stats) if row["verified_claim_events"] == 0]
    candidates = gaps or list(range(len(stats)))
    if (len(gaps) == 1 and stats[gaps[0]]["attempt_count"] >= 2
            and history[-1]["index"] == gaps[0]):
        candidates = [i for i in range(len(stats)) if i != gaps[0]]
    return min(candidates, key=lambda i: (stats[i]["last_attempt_at_epoch"], i))


def summarize_study_progress(goal: Mapping[str, object]) -> dict[str, object]:
    """Show evidence coverage, never equating a source or attempt with mastery."""
    plan = goal.get("study_plan") or []
    stats = study_step_statistics(goal)
    steps = []
    for title, row in zip(plan, stats):
        attempts = row["attempt_count"]
        evidence = row["verified_claim_events"]
        status = ("not_attempted" if not attempts else
                  "needs_evidence" if not evidence else "some_verified_evidence")
        steps.append({"title": title, "status": status,
                      "attempt_count": attempts, "verified_claim_events": evidence})
    return {"step_count": len(plan),
            "steps_with_evidence": sum(row["status"] == "some_verified_evidence" for row in steps),
            "steps": steps}
