"""Check one responsible-disclosure claim against two official documents."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .learning_goals import LearningGoalStore


TOPIC = "White hat hacker practices and responsible disclosure"
FOCUS = "Responsible vulnerability disclosure"
CLAIM = ("Vulnerability reports should provide sufficient detail for verification; "
         "disclosure policies help the public find where to send them.")
_SUPPORT = (
    ("S1", "report_details", "cheatsheetseries.owasp.org",
     "https://cheatsheetseries.owasp.org/cheatsheets/Vulnerability_Disclosure_Cheat_Sheet.html",
     re.compile(r"\bprovide\s+sufficient\s+details\s+to\s+allow\s+the\s+vulnerabilities\s+"
                r"to\s+be\s+verified\s+and\s+reproduced\b", re.IGNORECASE)),
    ("S2", "report_destination", "www.cisa.gov",
     "https://www.cisa.gov/news-events/news/"
     "cisa-issues-final-vulnerability-disclosure-policy-directive-federal-agencies",
     re.compile(r"\bmake\s+it\s+easier\s+for\s+the\s+public\s+to\s+know\s+"
                r"where\s+to\s+send\s+a\s+report\b", re.IGNORECASE)),
)


def assess_disclosure_claim(root: Path, goal_id: str, artifact: Path) -> dict:
    """Validate current permission, saved source bytes, and both exact clauses."""
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
        host = row.get("host")
        if (not isinstance(text, str) or not 40 <= len(text) <= 80_000
                or not isinstance(host, str)
                or not isinstance(row.get("retrieved_at"), str)
                or row.get("verified") is not False
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != row.get("content_sha256")
                or host in documents):
            raise ValueError("Saved document text, digest, or host is invalid")
        documents[host] = row
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
        "schema": "sira.learning_goal_disclosure_claim_assessment.v1",
        "status": "joint_source_supported" if len(support) == 2 else "insufficient_joint_support",
        "learning_goal_id": goal_id, "claim": CLAIM,
        "support_mode": "two_complementary_official_source_components",
        "source_artifact": str(path), "source_artifact_sha256": artifact_sha,
        "support": support, "knowledge_written": False,
        "verified_claims_recorded": False, "api_requests": 0,
        "metered_model_requests": 0, "promotion_performed": False,
    }
