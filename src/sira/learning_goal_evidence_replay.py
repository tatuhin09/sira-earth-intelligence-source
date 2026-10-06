"""Turn saved public research abstracts into auditable, unverified quotes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from .learning_goal_source_review import review_discovered_sources
from .learning_goals import LearningGoalStore
from .reading import Document
from .reports import evidence_quotes
from .storage import write_json


_REPORT_NAME = re.compile(r"(lg_[0-9a-f]{32})_[0-9a-f]{32}\.json\Z")
_MAX_REPORT_BYTES = 2 * 1024 * 1024


def _load_report(root: Path, filename: str) -> tuple[dict[str, Any], str]:
    if not isinstance(filename, str) or not (match := _REPORT_NAME.fullmatch(filename)):
        raise ValueError("Invalid learning goal report filename")
    directory = root / "memory" / "learning_goal_reports"
    path = directory / filename
    if (directory.is_symlink() or path.is_symlink() or not path.is_file()
            or path.resolve().parent != directory.resolve()
            or path.stat().st_size > _MAX_REPORT_BYTES):
        raise ValueError("Learning goal report missing or unsafe")
    raw = path.read_bytes()
    try:
        report = json.loads(raw)
        if (not isinstance(report, dict)
                or report.get("schema") != "sira.learning_goal_research.v1"
                or report.get("status") != "completed"
                or report.get("learning_goal_id") != match.group(1)
                or report.get("topic") != LearningGoalStore(root).get(match.group(1))["topic"]
                or not isinstance(report.get("research"), dict)):
            raise ValueError("Learning goal report does not match its goal")
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise ValueError("Invalid learning goal report") from exc
    return report, hashlib.sha256(raw).hexdigest()


def replay_learning_goal_evidence(root: Path, report_filename: str) -> dict[str, Any]:
    """Offline replay. Exact abstract quotes are not independently verified claims."""
    root = Path(root).resolve()
    report, source_sha = _load_report(root, report_filename)
    output_dir = root / "memory" / "learning_goal_evidence"
    output_path = output_dir / f"{report['learning_goal_id']}_{source_sha}.json"
    if output_dir.is_symlink() or output_path.is_symlink():
        raise ValueError("Learning goal evidence path is unsafe")
    if output_path.exists():
        try:
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            if (saved.get("schema") != "sira.learning_goal_quote_evidence.v1"
                    or saved.get("source_report_sha256") != source_sha
                    or saved.get("learning_goal_id") != report["learning_goal_id"]):
                raise ValueError("Existing evidence has incompatible provenance")
            saved["artifact"] = str(output_path)
            return saved
        except (UnicodeError, json.JSONDecodeError, AttributeError) as exc:
            raise ValueError("Existing evidence cannot be replayed") from exc

    research = report["research"]
    review = review_discovered_sources(report["topic"], research)
    quotes: list[dict[str, Any]] = []
    used_hosts: set[str] = set()
    used_sources = 0
    for item in review["candidates"]:
        if item["overlap"] != "high":
            continue
        kind, index = item["provider_kind"], item["result_index"]
        row = research[kind]["results"][index]
        abstract = row.get("abstract")
        if not isinstance(abstract, str) or not abstract.strip() or len(abstract) > 100_000:
            continue
        try:
            document = Document(row["url"], abstract, row["retrieved_at"])
        except (KeyError, TypeError, ValueError, UnicodeError):
            continue
        source_id = f"S{used_sources + 1}"
        found = evidence_quotes(report["topic"], source_id, document)
        if not found:
            continue
        used_sources += 1
        used_hosts.add(urlsplit(document.url).hostname or "")
        for quote in found:
            if abstract[quote["start"]:quote["end"]] != quote["quote"]:
                raise ValueError("Quote integrity failure")
            quotes.append({
                **quote,
                "provider_kind": kind,
                "source_index": index,
                "content_origin": "search_metadata_abstract",
                "verified": False,
            })

    status = ("insufficient_relevant_sources" if not used_sources else
              "insufficient_independent_sources" if len(used_hosts) < 2 else
              "quotes_need_claim_verification")
    result = {
        "schema": "sira.learning_goal_quote_evidence.v1",
        "status": status,
        "learning_goal_id": report["learning_goal_id"],
        "topic": report["topic"],
        "source_report_sha256": source_sha,
        "source_review": review,
        "quote_source_count": used_sources,
        "distinct_quote_host_count": len(used_hosts),
        "quotes": quotes,
        "verified_claims_recorded": False,
        "api_requests": 0,
        "metered_model_requests": 0,
        "promotion_performed": False,
    }
    write_json(output_path, result)
    result["artifact"] = str(output_path)
    return result
