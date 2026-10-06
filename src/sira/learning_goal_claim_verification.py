"""Narrow deterministic verification of a two-part official documentation claim.

This proves only the specified test-discovery statement. Other topics and
claims require their own evidence and verifier; no model or network is used.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .knowledge_consolidation import (
    KnowledgeConsolidationStore, auto_key, stable_evidence_id,
)
from .learning_goal_official_sources import _validate_saved
from .learning_goal_relevance_replay import replay_official_learning_quotes
from .learning_goals import LearningGoalStore
from .runtime import RuntimeStateStore


CLAIM = "Python unittest and pytest both support test discovery."
_SUPPORT = {
    "S1": ("docs.python.org", re.compile(r"\bunittest supports simple test discovery\.")),
    "S2": ("docs.pytest.org", re.compile(
        r"\bpytest discovers all tests following its conventions for python test discovery\b"
    )),
}


def _read_original(root: Path, goal_id: str, expected_sha: str) -> dict:
    directory = root / "memory" / "learning_goal_documents"
    path = directory / f"{goal_id}_python_testing_v1.json"
    if (directory.parent.is_symlink() or directory.is_symlink() or path.is_symlink()
            or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024):
        raise ValueError("Official source artifact missing or unsafe")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("Official source artifact changed after quote replay")
    try:
        return _validate_saved(json.loads(raw), goal_id)
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise ValueError("Official source artifact is invalid") from exc


def assess_discovery_claim(root: Path, goal_id: str) -> dict:
    """Read existing evidence only; demand exact clauses from both hosts."""
    root = Path(root).resolve()
    goal = LearningGoalStore(root).get(goal_id)
    if goal["topic"].casefold() != "python software testing" or goal["status"] != "active":
        raise ValueError("Learning goal is not active for this claim")
    source_path = root / "memory" / "learning_goal_documents" / f"{goal_id}_python_testing_v1.json"
    if (source_path.is_symlink() or not source_path.is_file()
            or source_path.stat().st_size > 2 * 1024 * 1024):
        raise ValueError("Official source artifact not available or unsafe")
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    replay_path = (root / "memory" / "learning_goal_evidence" /
                   f"{goal_id}_{source_sha}_official_relevant_v3.json")
    if replay_path.is_symlink() or not replay_path.is_file():
        raise ValueError("Run the offline quote replay before assessing claims")
    replay = replay_official_learning_quotes(root, goal_id)
    original = _read_original(root, goal_id, replay["source_report_sha256"])
    support = []
    for source_id, (host, pattern) in _SUPPORT.items():
        source = original["sources"][int(source_id[1:]) - 1]
        if urlsplit(source["url"]).hostname != host:
            raise ValueError("Official source host mismatch")
        for quote in replay["quotes"]:
            if quote["source_id"] != source_id:
                continue
            normalized = " ".join(quote["quote"].casefold().split())
            if not pattern.search(normalized):
                continue
            if (source["text"][quote["start"]:quote["end"]] != quote["quote"]
                    or quote["content_sha256"] != source["content_sha256"]
                    or quote["url"] != source["url"]):
                raise ValueError("Claim support quote does not match its source")
            support.append({
                "source_id": source_id, "url": source["url"], "host": host,
                "component": "unittest_discovery" if source_id == "S1" else "pytest_discovery",
                "quote_sha256": hashlib.sha256(quote["quote"].encode("utf-8")).hexdigest(),
                "content_sha256": source["content_sha256"],
                "retrieved_at": source["retrieved_at"],
                "start": quote["start"], "end": quote["end"],
            })
            break
    return {
        "schema": "sira.learning_goal_discovery_claim_assessment.v1",
        "status": "joint_source_supported" if len(support) == 2 else "insufficient_joint_support",
        "learning_goal_id": goal_id, "claim": CLAIM,
        "support_mode": "two_complementary_official_source_components",
        "source_report_sha256": source_sha, "support": support,
        "knowledge_written": False, "verified_claims_recorded": False,
        "api_requests": 0, "metered_model_requests": 0,
        "promotion_performed": False,
    }


def record_discovery_claim(root: Path, goal_id: str) -> dict:
    """Record this specifically checked claim in existing evidence-gated memory."""
    root = Path(root).resolve()
    runtime = RuntimeStateStore(root).status()
    if (runtime["desired_state"] != "off" or runtime["worker_alive"]
            or runtime["state_health"] not in {"ok", "missing_default"}):
        raise ValueError("Stop the runtime before recording learning evidence")
    report = assess_discovery_claim(root, goal_id)
    if report["status"] != "joint_source_supported":
        raise ValueError("Both official source components are required")
    store = KnowledgeConsolidationStore(root)
    key = auto_key(CLAIM)
    inserted = 0
    for row in report["support"]:
        inserted += int(store.record_evidence(
            key, CLAIM,
            evidence_id=stable_evidence_id(report["source_report_sha256"], CLAIM, row["url"]),
            source_id=row["source_id"], source_url=row["url"], confidence=.85,
            verifier_kind="deterministic_joint_official_docs_v1:" + row["component"],
            evidence_sha256=report["source_report_sha256"],
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
