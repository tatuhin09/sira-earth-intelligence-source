"""Conservative local routing for owner learning goals from desktop chat."""
from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import urlsplit

from .desktop_verified_knowledge import _terms
from .knowledge_consolidation import KnowledgeConsolidationStore
from .learning_goals import LearningGoalStore
from .learning_goal_progress import summarize_study_progress
from .learning_goal_evidence_progress import verified_study_step_evidence
from .learning_goal_skill_candidates import advisory_skill_candidates


_EXPLICIT = re.compile(r"^\s*(?:goal|learn|শেখার\s+লক্ষ্য)\s*[:：]\s*(.*?)\s*$", re.I | re.S)
_DAILY = re.compile(
    r"^\s*(?:sira,?\s+)?learn\s+(?:about\s+)?(.+?)\s+"
    r"(?:every\s+day|daily|long[\s-]+term)\s*[.!]?\s*$",
    re.I | re.S,
)
_BANGLA_DAILY = re.compile(r"^\s*শেখো\s+(.+?)\s+প্রতিদিন\s*[।.!]?\s*$", re.S)
_MANAGE_PREFIX = re.compile(r"^\s*goal\s+(show|plan|pause|resume)\b", re.I)
_MANAGE = re.compile(
    r"^\s*goal\s+(show|plan|pause|resume)\s+([^\s:]+)(?:\s*:\s*(.*))?\s*$",
    re.I | re.S,
)


def _relevant_verified_claims(root: Path, plan: list[str]) -> list[str]:
    """Find fresh two-host claims without changing historical goal counters."""
    db = root / "memory" / "sira_knowledge.sqlite3"
    if db.is_symlink() or not db.is_file():
        return []
    claims: dict[str, str] = {}
    try:
        store = KnowledgeConsolidationStore(root)
        for step in plan:
            terms = _terms(step)
            if len(terms) < 2:
                continue
            for row in store.search(step, limit=3):
                overlap = terms & _terms(row["claim_text"])
                if (len(overlap) < 2 or len(overlap) / len(terms) < .5
                        or int(row["host_count"]) < 2):
                    continue
                key = str(row["knowledge_key"])
                urls = store.verified_source_urls(key, limit=4)
                if len({urlsplit(url).hostname for url in urls}) < 2:
                    continue
                claims.setdefault(key, str(row["claim_text"]))
    except (OSError, ValueError, RuntimeError):
        return []
    return list(claims.values())


def _management(root: Path, message: str) -> tuple[str, dict | None, str] | None:
    store = LearningGoalStore(root)
    if message.strip().casefold() in {"goals", "goals list", "my learning goals"}:
        try:
            rows = store.list()
        except ValueError as exc:
            return f"Learning goals unavailable: {exc}.", None, "local_learning_goal_error"
        if not rows:
            return "No learning goals are saved.", None, "local_learning_goals"
        lines = [f"Saved learning goals: {len(rows)} (showing {min(len(rows), 12)})."]
        for row in rows[:12]:
            lines.append(f"- {row['goal_id']}: {row['topic'][:80]} "
                         f"({row['status']}, {row['priority']})")
        return "\n".join(lines), None, "local_learning_goals"

    if _MANAGE_PREFIX.match(message) is None:
        return None
    match = _MANAGE.fullmatch(message)
    if match is None:
        return "Invalid goal command. Use Goal show/plan/pause/resume followed by a goal ID.", None, "local_learning_goal_error"
    action, goal_id, plan_text = match.group(1).lower(), match.group(2), match.group(3)
    if (action == "plan" and plan_text is None) or (action != "plan" and plan_text is not None):
        return "Invalid goal command format.", None, "local_learning_goal_error"
    try:
        if action == "show":
            goal = store.get(goal_id)
            plan = goal.get("study_plan") or []
            details = (f"Goal {goal_id}: {goal['topic']}. Status: {goal['status']}. "
                       f"Priority: {goal['priority']}. Public research: "
                       f"{'allowed' if goal['public_research_allowed'] else 'disabled'}. "
                       f"Last outcome: {goal.get('last_outcome', 'none')}.")
            if plan:
                plan_text = " | ".join(plan)
                details += ("\nStudy plan: " + plan_text if len(plan_text) <= 1200
                            else f"\nStudy plan: {len(plan)} steps (listed below)")
                details += f"\nNext: {plan[goal['study_next_index']]}"
                progress = summarize_study_progress(goal)
                local_evidence = verified_study_step_evidence(root, goal)
                for step in progress["steps"]:
                    if (step["status"] != "some_verified_evidence"
                            and local_evidence.get(step["title"])):
                        step["status"] = "some_verified_local_evidence"
                progress["steps_with_evidence"] = sum(
                    step["status"] in {"some_verified_evidence", "some_verified_local_evidence"}
                    for step in progress["steps"]
                )
                details += (f"\nProgress: {progress['steps_with_evidence']} of "
                            f"{progress['step_count']} steps have some verified claim evidence.")
                followups = goal.get("research_followups") or {}
                shown_hints = 0
                for step in progress["steps"]:
                    state = step["status"].replace("_", " ")
                    details += f"\n- {step['title']}: {state} ({step['attempt_count']} attempts)"
                    hint = (followups.get(step["title"]) or {}).get("hint")
                    if hint and shown_hints < 2:
                        details += f"; unverified title hint: {step['title']} {hint['term']}"
                        shown_hints += 1
                    questions = (followups.get(step["title"]) or {}).get("questions") or []
                    pending = [question for question in questions if not question["attempted"]]
                    if pending:
                        details += (f"\n  {len(pending)} queued, unverified research questions: "
                                    + ", ".join(step["title"] + " " + question["term"]
                                                for question in pending[:3]))
                local_claims = _relevant_verified_claims(root, plan)
                if local_claims:
                    details += (f"\nRelevant verified local claims: {len(local_claims)} "
                                "(topic match; not full mastery).")
                    for claim in local_claims[:2]:
                        details += f"\n- Verified: {claim[:250]}"
                practice = advisory_skill_candidates(root, goal, limit=3)
                if practice:
                    details += (f"\nPractice candidates (not demonstrated skills): "
                                f"{len(practice)} shown.")
                    for item in practice:
                        details += f"\n- {item['focus']}: apply verified claim in a bounded exercise."
            return details, goal, "local_learning_goals"
        if action == "plan":
            goal = store.set_plan(goal_id, [step.strip() for step in plan_text.split("|")])
            return (f"Study plan saved for {goal_id}: {len(goal['study_plan'])} steps. "
                    f"Next: {goal['study_plan'][goal['study_next_index']]}.",
                    goal, "local_learning_goal")
        goal = store.set_status(goal_id, "paused" if action == "pause" else "active")
        return f"Goal {goal_id} is {goal['status']}.", goal, "local_learning_goal"
    except ValueError as exc:
        return f"Learning goal unchanged: {exc}.", None, "local_learning_goal_error"


def _request(message: str) -> tuple[str, str, bool] | None:
    match = _EXPLICIT.fullmatch(message)
    if match is None:
        match = _DAILY.fullmatch(message)
        if match is None:
            match = _BANGLA_DAILY.fullmatch(message)
            if match is None:
                return None
    parts = [part.strip() for part in match.group(1).split("|")]
    if not 1 <= len(parts) <= 3 or not parts[0]:
        raise ValueError("missing topic or too many options")
    topic = parts[0]
    priority = "normal"
    public = False
    seen: set[str] = set()
    for part in parts[1:]:
        option = re.fullmatch(r"(priority|public)\s*=\s*([a-z]+)", part, re.I)
        if option is None:
            raise ValueError("unknown option")
        key, value = option.group(1).lower(), option.group(2).lower()
        if key in seen:
            raise ValueError("duplicate option")
        seen.add(key)
        if key == "priority":
            if value not in {"normal", "high"}:
                raise ValueError("priority must be normal or high")
            priority = value
        else:
            if value not in {"yes", "no"}:
                raise ValueError("public must be yes or no")
            public = value == "yes"
    return topic, priority, public


def learning_goal_chat_reply(root: Path, message: str) -> tuple[str, dict | None, str] | None:
    """Return a local reply for deliberate goal requests; leave other chat alone."""
    managed = _management(root, message)
    if managed is not None:
        return managed
    try:
        request = _request(message)
    except ValueError as exc:
        return (f"Invalid learning goal request: {exc}. "
                "Use Goal: topic | priority=high | public=yes."), None, "local_learning_goal_error"
    if request is None:
        return None
    topic, priority, public = request
    try:
        goal = LearningGoalStore(root).create(
            topic, priority=priority, public_research_allowed=public
        )
    except ValueError as exc:
        return (f"Learning goal was not saved: {exc}. "
                "An existing goal's policy can be changed with the goals controls."), None, "local_learning_goal_error"
    permission = "allowed" if public else "disabled"
    return (f"Learning goal saved (or already present): {goal['topic']}. "
            f"ID: {goal['goal_id']}. Status: {goal['status']}. Priority: {goal['priority']}. "
            f"Public research: {permission}. "
            "It is eligible for autonomous study when active and SIRA is on, "
            "subject to its retry interval and resource limits."), goal, "local_learning_goal"
