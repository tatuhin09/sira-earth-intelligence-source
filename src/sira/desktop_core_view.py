"""Small, read-only desktop presentation of the A83 SIRA Core state."""
from __future__ import annotations

from pathlib import Path

from .learning_goal_adaptive import preview_next_question
from .learning_goal_evidence_progress import verified_study_step_evidence
from .learning_goals import LearningGoalStore


def compact_core_state(root: Path, state: dict) -> dict:
    """Bound UI data, keeping authority and evidence states descriptive only."""
    model = state["self_model"]
    goal_rows = []
    store = LearningGoalStore(root)
    for row in state["active_goals"].get("goals", [])[:5]:
        goal_id = row["goal_id"]
        try:
            goal = store.get(goal_id)
            next_step = preview_next_question(root, goal)
            evidence = verified_study_step_evidence(root, goal)
        except (ValueError, OSError):
            goal, next_step, evidence = {}, None, {}
        goal_rows.append({
            "goal_id": goal_id, "topic": row["topic"],
            "priority": row["priority"], "last_outcome": goal.get("last_outcome"),
            "verified_steps": sum(bool(item) for item in evidence.values()),
            "total_steps": len(goal.get("study_plan") or []),
            "next_question": (next_step or {}).get("question"),
            "next_reason": (next_step or {}).get("reason"),
        })
    capabilities = [{"name": name, "state": detail.get("state"),
                     "scope": detail.get("scope"),
                     "reason": detail.get("reason"),
                     "evidence_refs": [entry.get("ref") for entry in detail.get("evidence", [])[:2]]}
                    for name, detail in list(model["capabilities"].items())[:20]]
    history = state["task_history"]
    pending = state["pending_work"]
    return {
        "schema": "sira.desktop_core_view.v1", "identity": state["identity"],
        "runtime": state["runtime"], "release": state["release"],
        "current_direct_user_task": state["current_direct_user_task"],
        "pending_work": [{key: task.get(key) for key in
                          ("task_id", "intent", "origin", "status", "route", "reason", "priority", "verification_status", "artifact_refs")}
                         for task in pending[:8]],
        "last_task": ({key: history[-1].get(key) for key in
                       ("task_id", "intent", "status", "route", "reason", "verification_status", "artifact_refs", "evidence_refs")}
                      if history else None),
        "preemption": state["preemption"],
        "learning": {"active_goal_count": state["active_goals"].get("active_goal_count", 0),
                     "goals": goal_rows},
        "knowledge": {key: state["verified_memory"].get(key) for key in
                      ("verified_count", "stale_sample_count", "sampled_count", "conflicted_count", "bootstrap", "integrity")},
        "capabilities": capabilities, "gaps": state["capability_gaps"][:5],
        "providers": {"known_count": state["providers"].get("known_count", 0),
                      "available_count": state["providers"].get("available_count", 0),
                      "cooling_count": state["providers"].get("cooling_count", 0),
                      "entries": [{key: item.get(key) for key in
                                   ("provider_id", "state", "health", "cost_class", "credential_state")}
                                  for item in state["providers"].get("entries", [])[:16]]},
        "workers": state["workers"], "recent_outcome": state["recent_outcome"],
        "recent_failures": model.get("recent_failures", [])[:3],
        "recent_improvements": model.get("recent_improvements", [])[:3],
        "authority_granted": False, "promotion_authorized": False,
        "paid_spending_authorized": False, "skill_activated": False,
        "resource_usage": state["resource_usage"],
    }


def verified_memory_text(decision: dict) -> tuple[str, list[dict]]:
    """Teach one narrow fact with the exact two-host provenance from A79."""
    hit = decision["results"][0]
    sources = hit["provenance"][:4]
    urls = list(dict.fromkeys(row["source_url"] for row in sources))
    reply = ("One verified claim relevant to your question: " + hit["claim"] +
             "\nSources:\n" + "\n".join("- " + url for url in urls) +
             "\nThis one claim does not cover the whole topic. Further research "
             "and verification are needed for a broader explanation.")
    return reply, [{"knowledge_key": hit["knowledge_key"], "sources": sources,
                    "freshness": hit["freshness"], "confidence": hit["confidence"]}]


def operational_text(state: dict, message: str) -> str:
    runtime = state["runtime"]
    lower = message.casefold()
    if any(cue in lower for cue in ("start sira", "stop sira", "self on", "self off", "চালু কর", "বন্ধ কর")):
        return ("Runtime control from free-form chat is disabled. Use the explicit "
                "Start SIRA or Stop SIRA control button.")
    if "capabilit" in lower or "gap" in lower or "সক্ষমতা" in lower:
        gaps = state["capability_gaps"][:3]
        summary = ", ".join(str(row["capability"]) + ": " + str(row["state"]) for row in gaps)
        return "SIRA's evidence-backed capability gaps: " + (summary or "none recorded") + "."
    goal_count = state["active_goals"].get("active_goal_count", 0)
    knowledge = state["verified_memory"].get("verified_count", 0)
    return (f"SIRA runtime is {runtime.get('effective_state')} "
            f"(desired: {runtime.get('desired_state')}, generation: {runtime.get('generation')}, "
            f"health: {runtime.get('state_health')}). "
            f"Active learning goals: {goal_count}; verified knowledge: {knowledge}. "
            f"Last outcome: {state.get('recent_outcome') or 'none recorded'}. "
            f"Release ready: {bool(state['release'].get('release_ready'))}.")
