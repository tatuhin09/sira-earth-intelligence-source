"""Advisory practice candidates grounded in rechecked goal evidence.

A verified claim is evidence for study, not evidence of a successful skill
application. The fixed A74 provenance exercise may be run locally; it does
not execute a domain skill, write skill evidence, or activate a skill.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .knowledge_consolidation import KnowledgeConsolidationStore
from .learning_goal_evidence_progress import verified_study_step_evidence


def advisory_skill_candidates(root: Path, goal: Mapping[str, object], *,
                              limit: int = 3) -> list[dict[str, object]]:
    """Link exact saved source evidence to bounded local provenance practice."""
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("candidate limit must be 1..10")
    goal_id, plan = goal.get("goal_id"), goal.get("study_plan")
    if (not isinstance(goal_id, str) or not isinstance(plan, list)
            or not all(isinstance(step, str) for step in plan)):
        return []
    root = Path(root).resolve()
    db = root / "memory" / "sira_knowledge.sqlite3"
    if db.is_symlink() or not db.is_file():
        return []
    try:
        evidence = verified_study_step_evidence(root, goal)
        store = KnowledgeConsolidationStore(root)
        candidates = []
        for focus in plan:
            verified_claims = set(evidence.get(focus, []))
            if not verified_claims:
                continue
            for row in store.search(focus, limit=50):
                claim = str(row["claim_text"])
                if (claim not in verified_claims or row["status"] != "active"
                        or row["freshness"] != "fresh" or row["host_count"] < 2):
                    continue
                knowledge_key = str(row["knowledge_key"])
                urls = store.verified_source_urls(knowledge_key, limit=4)
                parsed = [urlsplit(url) for url in urls]
                if (any(url.scheme != "https" or url.username or url.password
                        or not url.hostname for url in parsed)
                        or len({url.hostname for url in parsed}) < 2):
                    continue
                digest = hashlib.sha256(
                    (goal_id + "\0" + focus + "\0" + knowledge_key).encode("utf-8")
                ).hexdigest()[:24]
                candidates.append({
                    "candidate_id": "lsc_" + digest,
                    "goal_id": goal_id,
                    "focus": focus,
                    "claim": claim,
                    "knowledge_key": knowledge_key,
                    "source_urls": urls,
                    "status": "needs_demonstrated_application",
                    "practice_checklist": [
                        "Choose an authorized, bounded example for this study focus.",
                        "Apply the claim and record the observed result and sources.",
                        "Check the result independently before recording a successful application.",
                    ],
                    "executable": True,
                    "execution_mode": "isolated_evidence_trace_v1",
                    "skill_activated": False,
                    "authority_granted": False,
                })
                if len(candidates) >= limit:
                    return candidates
        return candidates
    except (OSError, ValueError, RuntimeError, TypeError):
        # Corrupt or unavailable evidence never becomes a skill proposal.
        return []
