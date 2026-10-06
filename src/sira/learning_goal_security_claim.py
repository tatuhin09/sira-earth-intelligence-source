"""Verify one narrow risk-assessment and threat-modeling claim offline.

The stored, independently hosted document text must contain both exact
support components. This verifier does not generalize to other claims.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .knowledge_consolidation import (
    KnowledgeConsolidationStore, auto_key, stable_evidence_id,
)
from .learning_goals import LearningGoalStore
from .runtime import RuntimeStateStore


TOPIC = "Cybersecurity fundamentals and defense"
FOCUS = "Cybersecurity risk assessment and threat modeling"
CLAIM = ("Risk assessment evaluates risks and considers incident consequences, "
         "while threat modeling identifies potential security issues during design.")
_SUPPORT = (
    ("S1", "risk_evaluation", "www.cisa.gov",
     "https://www.cisa.gov/resources-tools/resources/risk-assessment-methodologies",
     re.compile(r"\brisk\s+assessment\s+involves\s+the\s+evaluation\s+of\s+risks\s+"
                r"taking\s+into\s+consideration\s+the\s+potential\s+direct\s+and\s+indirect\s+"
                r"consequences\s+of\s+an\s+incident\b", re.IGNORECASE)),
    ("S2", "design_security_issues", "cheatsheetseries.owasp.org",
     "https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html",
     re.compile(r"\bthreat\s+modeling\s+seeks\s+to\s+identify\s+potential\s+security\s+"
                r"issues\s+during\s+the\s+design\s+phase\.", re.IGNORECASE)),
)


def assess_security_risk_claim(root: Path, goal_id: str, artifact: Path) -> dict:
    """Check saved source bytes and exact clauses; never write knowledge."""
    root = Path(root).resolve()
    goal = LearningGoalStore(root).get(goal_id)
    if (goal["topic"] != TOPIC or goal["status"] != "active"
            or goal["public_research_allowed"] is not True
            or FOCUS not in goal.get("study_plan", [])):
        raise ValueError("This active public learning goal and focus are required")
    directory = root / "memory" / "learning_goal_documents"
    path = Path(artifact)
    if (directory.parent.is_symlink() or directory.is_symlink()
            or path.parent != directory or path.is_symlink()
            or not re.fullmatch(re.escape(goal_id) + r"_[0-9a-f]{32}\.json", path.name)
            or not path.is_file() or not 0 < path.stat().st_size <= 1_000_000):
        raise ValueError("Saved learning document artifact is missing or unsafe")
    raw = path.read_bytes()
    artifact_sha = hashlib.sha256(raw).hexdigest()
    try:
        saved = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise ValueError("Saved learning document artifact is invalid") from exc
    if (not isinstance(saved, dict)
            or saved.get("schema") != "sira.learning_goal_general_documents.v1"
            or saved.get("learning_goal_id") != goal_id
            or saved.get("topic") != TOPIC
            or saved.get("research_query") != FOCUS
            or saved.get("status") != "quotes_need_claim_verification"
            or saved.get("verified_claims_recorded") is not False
            or not isinstance(saved.get("documents"), list)
            or len(saved["documents"]) != 2):
        raise ValueError("Saved documents are not eligible for this claim")

    documents = {}
    for row in saved["documents"]:
        if not isinstance(row, dict):
            raise ValueError("Saved document is invalid")
        text = row.get("text")
        if (not isinstance(text, str) or not 40 <= len(text) <= 80_000
                or not isinstance(row.get("retrieved_at"), str)
                or row.get("verified") is not False
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != row.get("content_sha256")):
            raise ValueError("Saved document text or digest is invalid")
        if row.get("host") in documents:
            raise ValueError("Independent official hosts are required")
        documents[row.get("host")] = row

    if set(documents) != {item[2] for item in _SUPPORT}:
        raise ValueError("Independent official hosts are required")
    support = []
    for source_id, component, host, url, pattern in _SUPPORT:
        row = documents[host]
        if row.get("url") != url:
            raise ValueError("Official source URL mismatch")
        match = pattern.search(row["text"])
        if match is None:
            continue
        quote = row["text"][match.start():match.end()]
        support.append({
            "source_id": source_id, "component": component,
            "host": host, "url": url,
            "start": match.start(), "end": match.end(),
            "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            "content_sha256": row["content_sha256"],
            "retrieved_at": row["retrieved_at"],
        })
    return {
        "schema": "sira.learning_goal_security_risk_claim_assessment.v1",
        "status": "joint_source_supported" if len(support) == 2 else "insufficient_joint_support",
        "learning_goal_id": goal_id, "claim": CLAIM,
        "support_mode": "two_complementary_official_source_components",
        "source_artifact": str(path), "source_artifact_sha256": artifact_sha,
        "support": support, "knowledge_written": False,
        "verified_claims_recorded": False, "api_requests": 0,
        "metered_model_requests": 0, "promotion_performed": False,
    }


def record_security_risk_claim(root: Path, goal_id: str, artifact: Path, *,
                               expected_artifact_sha256: str) -> dict:
    """Record only the two-component claim under the stopped runtime."""
    root = Path(root).resolve()
    runtime = RuntimeStateStore(root).status()
    if (runtime["desired_state"] != "off" or runtime["worker_alive"]
            or runtime["state_health"] not in {"ok", "missing_default"}):
        raise ValueError("Stop the runtime before recording learning evidence")
    report = assess_security_risk_claim(root, goal_id, artifact)
    if (not isinstance(expected_artifact_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_artifact_sha256)
            or report["source_artifact_sha256"] != expected_artifact_sha256):
        raise ValueError("Learning document artifact changed after assessment")
    if report["status"] != "joint_source_supported":
        raise ValueError("Both official source components are required")
    store = KnowledgeConsolidationStore(root)
    key = auto_key(CLAIM)
    inserted = 0
    for row in report["support"]:
        inserted += int(store.record_evidence(
            key, CLAIM,
            evidence_id=stable_evidence_id(report["source_artifact_sha256"], CLAIM, row["url"]),
            source_id=row["source_id"], source_url=row["url"],
            confidence=.85,
            verifier_kind="deterministic_joint_official_security_v1:" + row["component"],
            evidence_sha256=report["source_artifact_sha256"],
            retrieved_at=row["retrieved_at"], verified=True,
        ))
    decision = store.consolidate(key).to_dict()
    if decision["status"] != "consolidated" or decision["host_count"] < 2:
        raise ValueError("The verified claim was not consolidated")
    return {
        **report, "status": "consolidated", "knowledge_key": key,
        "new_evidence_count": inserted, "decision": decision,
        "knowledge_written": True, "verified_claims_recorded": True,
    }
