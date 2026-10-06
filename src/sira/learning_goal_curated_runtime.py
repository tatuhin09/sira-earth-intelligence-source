"""Run narrow official-source claim checks inside an authorized study cycle."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from .knowledge_consolidation import KnowledgeConsolidationStore, auto_key, stable_evidence_id
from .learning_goal_scope_claim import (
    FOCUS as SCOPE_FOCUS, TOPIC as SCOPE_TOPIC, assess_testing_scope_claim,
)
from .learning_goal_disclosure_claim import (
    FOCUS as DISCLOSURE_FOCUS, TOPIC as DISCLOSURE_TOPIC, assess_disclosure_claim,
)
from .learning_goal_security_claim import (
    FOCUS as RISK_FOCUS, TOPIC as RISK_TOPIC, assess_security_risk_claim,
)
from .learning_goals import LearningGoalStore


_ASSESSORS: dict[tuple[str, str], Callable[[Path, str, Path], dict]] = {
    (RISK_TOPIC, RISK_FOCUS): assess_security_risk_claim,
    (SCOPE_TOPIC, SCOPE_FOCUS): assess_testing_scope_claim,
    (DISCLOSURE_TOPIC, DISCLOSURE_FOCUS): assess_disclosure_claim,
}
_OFFICIAL_HOSTS = {
    (RISK_TOPIC, RISK_FOCUS): {"www.cisa.gov", "cheatsheetseries.owasp.org"},
    (SCOPE_TOPIC, SCOPE_FOCUS): {"cheatsheetseries.owasp.org", "www.cisa.gov"},
    (DISCLOSURE_TOPIC, DISCLOSURE_FOCUS): {"cheatsheetseries.owasp.org", "www.cisa.gov"},
}


def has_curated_official_focus(topic: str, focus: str) -> bool:
    """A known two-page route can run even when metadata providers are empty."""
    return (topic, focus) in _ASSESSORS


def verify_curated_saved_claim(root: Path, goal_id: str, topic: str,
                              focus: str, artifact: Path) -> dict:
    """Consolidate only an exact supported claim; never trust search metadata."""
    root = Path(root).resolve()
    assessor = _ASSESSORS.get((topic, focus))
    if assessor is None:
        return {"status": "unverified_source_statements", "verified_claim_count": 0,
                "claims": [], "metered_model_requests": 0}

    def allowed() -> bool:
        goal = LearningGoalStore(root).get(goal_id)
        return (goal["topic"] == topic and goal["status"] == "active"
                and goal["public_research_allowed"] is True
                and focus in goal.get("study_plan", []))

    if not allowed():
        return {"status": "research_permission_revoked", "verified_claim_count": 0,
                "claims": [], "metered_model_requests": 0}
    try:
        saved = json.loads(Path(artifact).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Saved official study artifact is invalid") from None
    documents = saved.get("documents") if isinstance(saved, dict) else None
    if (not isinstance(documents, list) or len(documents) != 2
            or not all(isinstance(row, dict) and isinstance(row.get("host"), str)
                       for row in documents)
            or {row.get("host") for row in documents} != _OFFICIAL_HOSTS[(topic, focus)]):
        return {"status": "unverified_source_statements", "verified_claim_count": 0,
                "claims": [], "metered_model_requests": 0}
    report = assessor(root, goal_id, artifact)
    if report["status"] != "joint_source_supported":
        return {"status": "unverified_source_statements", "verified_claim_count": 0,
                "claims": [], "metered_model_requests": 0}
    claim = report["claim"]
    support = report["support"]
    provenance = hashlib.sha256(json.dumps(
        sorted((row["url"], row["content_sha256"]) for row in support),
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    key = auto_key(claim)
    store = KnowledgeConsolidationStore(root)
    for row in support:
        if not allowed():
            return {"status": "research_permission_revoked", "verified_claim_count": 0,
                    "claims": [], "metered_model_requests": 0}
        store.record_evidence(
            key, claim,
            evidence_id=stable_evidence_id(provenance, claim, row["url"]),
            source_id=row["source_id"], source_url=row["url"],
            confidence=.85,
            verifier_kind="deterministic_joint_official_curated_v1:" + row["component"],
            evidence_sha256=provenance, retrieved_at=row["retrieved_at"],
            verified=True,
        )
    if not allowed():
        return {"status": "research_permission_revoked", "verified_claim_count": 0,
                "claims": [], "metered_model_requests": 0}
    decision = store.consolidate(key).to_dict()
    if decision["status"] != "consolidated" or decision["host_count"] < 2:
        raise ValueError("Curated source corroboration was not consolidated")
    return {
        "status": "verified_knowledge_recorded", "verified_claim_count": 1,
        "claims": [{"claim": claim, "knowledge_key": key,
                    "source_urls": [row["url"] for row in support]}],
        "metered_model_requests": 0,
    }
