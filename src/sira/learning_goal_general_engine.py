"""A77: one bounded question, readable sources, deterministic claim verification.

Search metadata proposes URLs only. The engine reads pages, preserves exact
bytes and contradictions, and records only an identical sentence supported by
two current, independent source hosts. It never runs practice or skills.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from itertools import combinations
import json
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from uuid import uuid4

from .knowledge_consolidation import KnowledgeConsolidationStore, auto_key, stable_evidence_id
from .learning_goal_general_learning import (
    _MAX_HTML_BYTES, _PUBLIC_PROVIDER_HOSTS, _fetch_public_document, _statements, _words,
)
from .trusted_hosts import load_trusted_hosts
from .learning_goal_official_sources import (
    _MAX_PDF_BYTES, _extract_public_document_text,
)
from .learning_goal_source_review import review_discovered_sources
from .learning_goals import LearningGoalStore
from .models import utc_now
from .reading import public_url
from .storage import write_json


_MAX_SOURCES = 3
_MAX_TEXT_CHARS = 80_000
_STALE_AFTER_DAYS = 180
_NEGATIONS = frozenset({"not", "never", "no"})


def _classify(host: str, trusted: frozenset = frozenset()) -> str | None:
    """Accept constrained public source classes, never a topic-specific route."""
    if host.endswith((".gov", ".edu")):
        return "official_primary"
    if host in {"arxiv.org", "doaj.org"}:
        return "scholarly_public"
    if host in _PUBLIC_PROVIDER_HOSTS:
        return "public_reference"
    if host in trusted:
        return "owner_trusted"  # owner-registered public documentation host
    return None


def _candidates(question: str, research: Mapping, trusted: frozenset = frozenset()) -> list[dict]:
    review = review_discovered_sources(question, research)
    selected, hosts = [], set()
    for item in review["candidates"]:
        if item["overlap"] not in {"high", "partial"}:
            continue
        row = research[item["provider_kind"]]["results"][item["result_index"]]
        try:
            url = public_url(row["url"])
            host = urlsplit(url).hostname
        except (KeyError, TypeError, ValueError, UnicodeError):
            continue
        source_class = _classify(host, trusted)
        if (urlsplit(url).scheme != "https" or source_class is None or host in hosts
                or len(url) > 2048):
            continue
        hosts.add(host)
        selected.append({"url": url, "host": host, "provider_kind": item["provider_kind"],
                         "result_index": item["result_index"],
                         "title": item["title"], "source_class": source_class})
    # Owner-registered documentation hosts outrank scholarly sources: for practical
    # topics official docs answer the question, and the owner chose them on purpose.
    rank = {"official_primary": 0, "owner_trusted": 1, "scholarly_public": 2,
            "public_reference": 3}
    return sorted(selected, key=lambda row: (rank[row["source_class"]], row["host"]))[:_MAX_SOURCES]


def has_general_source_gap(question: str, research: Mapping,
                           trusted_hosts: frozenset = frozenset()) -> bool:
    """Runtime fallback only when a relevant source lies beyond the old host set."""
    if (not isinstance(research, Mapping) or not isinstance(question, str)
            or not 1 <= len(question) <= 200):
        return False
    try:
        selected = _candidates(question, research, trusted_hosts)
    except (ValueError, KeyError, TypeError):
        return False
    return (len(selected) >= 2
            and any(row["host"] not in _PUBLIC_PROVIDER_HOSTS for row in selected))


def _polarity(sentence: str) -> tuple[str, bool]:
    tokens = _words(sentence)
    return " ".join(word for word in tokens if word not in _NEGATIONS), any(
        word in _NEGATIONS for word in tokens)


def assess_general_documents(question: str, documents: list[Mapping], *,
                             now: str | None = None,
                             trusted_hosts: frozenset = frozenset()) -> dict:
    """Read-only evidence assessment; exact sentences, two hosts, fresh text."""
    if not isinstance(question, str) or not 1 <= len(question) <= 200:
        raise ValueError("One bounded research question is required")
    if not isinstance(documents, list) or len(documents) > _MAX_SOURCES:
        raise ValueError("Too many evidence documents")
    current = datetime.now(timezone.utc) if now is None else datetime.fromisoformat(
        now.replace("Z", "+00:00"))
    if current.tzinfo is None:
        raise ValueError("Evidence time must have timezone")
    current = current.astimezone(timezone.utc)
    valid = []
    stale = []
    for row in documents:
        if not isinstance(row, Mapping):
            raise ValueError("Invalid source evidence")
        url = public_url(row.get("url"))
        host = urlsplit(url).hostname
        source_class = _classify(host, trusted_hosts)
        text = row.get("text")
        if (urlsplit(url).scheme != "https" or source_class is None
                or row.get("host") != host or row.get("verified") is not False
                or not isinstance(text, str) or not 40 <= len(text) <= _MAX_TEXT_CHARS
                or row.get("content_sha256") != hashlib.sha256(text.encode()).hexdigest()):
            raise ValueError("Source text or provenance mismatch")
        try:
            fetched = datetime.fromisoformat(row["retrieved_at"].replace("Z", "+00:00"))
            if fetched.tzinfo is None:
                raise ValueError("Timezone required")
            age = (current - fetched.astimezone(timezone.utc)).total_seconds()
        except (KeyError, TypeError, AttributeError):
            raise ValueError("Missing source retrieval time") from None
        if age < -300 or age > _STALE_AFTER_DAYS * 86400:
            stale.append({"url": url, "host": host, "retrieved_at": row["retrieved_at"]})
        valid.append(dict(row))
    result = {"status": "insufficient_independent_sources", "reason": "two_readable_hosts_required",
              "claim": None, "support": [], "contradictions": [],
              "unmatched_statements": [], "stale_sources": stale,
              "confidence": 0.0, "freshness": "stale" if stale else "fresh",
              "verified_at": None, "evidence_policy": "identical_sentence_two_hosts_v1"}
    if stale:
        result.update(status="stale_evidence", reason="source_retrieval_too_old")
        return result
    if len({row["host"] for row in valid}) < 2:
        return result

    sentences = [_statements(row["text"], question) for row in valid]
    # A contradiction in comparable, relevant sentences blocks even an
    # unrelated shared sentence in the same documents.
    for (i, left), (j, right) in combinations(enumerate(sentences), 2):
        lhs = {_polarity(s): text for s, text in left.items()}
        rhs = {_polarity(s): text for s, text in right.items()}
        for (base, negated), left_text in lhs.items():
            opposite = rhs.get((base, not negated))
            if opposite is not None and valid[i]["host"] != valid[j]["host"]:
                result["contradictions"].append({
                    "source_a": valid[i]["url"], "source_b": valid[j]["url"],
                    "statement_a": left_text, "statement_b": opposite,
                })
    if result["contradictions"]:
        result.update(status="contradictory_evidence", reason="opposing_source_statements")
        return result
    all_keys = set().union(*(set(rows) for rows in sentences))
    shared_keys = {key for key in all_keys if sum(key in rows for rows in sentences) >= 2}
    unmatched = [(row["url"], sentence)
                 for row, rows in zip(valid, sentences)
                 for key, sentence in rows.items() if key not in shared_keys]
    if shared_keys and unmatched:
        result.update(status="ambiguous_evidence",
                      reason="unmatched_relevant_source_statements",
                      unmatched_statements=[{"url": url, "statement": sentence}
                                            for url, sentence in unmatched[:3]])
        return result
    pairs = []
    for i, j in combinations(range(len(valid)), 2):
        for key in set(sentences[i]) & set(sentences[j]):
            pairs.append((sentences[i][key], valid[i], valid[j]))
    if not pairs:
        result.update(status="insufficient_corroboration", reason="no_shared_readable_claim")
        return result
    # The longest common complete sentence is more informative than a shared
    # section heading. Retain exactly one narrow claim per cycle.
    sentence, first, second = sorted(pairs, key=lambda row: (-len(_words(row[0])), row[0]))[0]
    result.update(status="corroborated", reason="exact_statement_two_independent_hosts",
                  claim=sentence, support=[first, second], confidence=.85,
                  verified_at=current.isoformat())
    return result


def run_general_learning(root: Path, goal_id: str, research: Mapping, *,
                         question: str | None = None,
                         fetcher: Callable[[str], tuple[str, str, bytes]] = _fetch_public_document) -> dict:
    """One local/free bounded research question; persist exact source provenance."""
    root = Path(root).resolve()
    goals = LearningGoalStore(root)
    goal = goals.get(goal_id)
    focus = question or goal["topic"]
    if (not isinstance(focus, str) or not 1 <= len(focus) <= 200
            or focus not in {goal["topic"], *(goal.get("study_plan") or [])}
            or goal["status"] != "active" or goal["public_research_allowed"] is not True
            or not isinstance(research, Mapping)):
        raise ValueError("Authorized, bounded learning goal required")
    trusted = load_trusted_hosts(root)
    selected = _candidates(focus, research, trusted)
    result = {"engine": "general_verified_learning_v1", "question": focus,
              "status": "insufficient_independent_sources", "reason": "two_relevant_sources_required",
              "verified_claim_count": 0, "claims": [], "contradictions": [],
              "unmatched_statements": [],
              "api_requests": 0, "metered_model_requests": 0,
              "resource_usage": {"api_requests": 0, "metered_model_requests": 0,
                                 "paid_requests": 0},
              "source_failures": [], "source_classes": [row["source_class"] for row in selected],
              "documents": [], "skill_activated": False, "promotion_performed": False}
    if len(selected) < 2:
        return result
    for item in selected:
        latest = goals.get(goal_id)
        if (latest["status"] != "active" or latest["public_research_allowed"] is not True
                or latest["topic"] != goal["topic"] or focus not in
                {latest["topic"], *(latest.get("study_plan") or [])}):
            result.update(status="research_permission_revoked", reason="owner_goal_changed")
            return result
        result["api_requests"] += 1
        result["resource_usage"]["api_requests"] += 1
        try:
            actual, content_type, raw = fetcher(item["url"])
            if (actual != item["url"] or not isinstance(raw, bytes) or not raw
                    or len(raw) > (_MAX_PDF_BYTES if content_type == "application/pdf"
                                   or raw.startswith(b"%PDF-") else _MAX_HTML_BYTES)):
                raise ValueError("Unexpected document response")
            text = _extract_public_document_text(raw, content_type)
            result["documents"].append({"url": item["url"], "host": item["host"],
                "discovery_provider": item["provider_kind"],
                "discovery_result_index": item["result_index"],
                "title": item["title"], "text": text,
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "retrieved_at": utc_now(), "verified": False})
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeError) as exc:
            code = (f"http_{exc.code}" if isinstance(exc, HTTPError) else
                    getattr(exc, "code", None) or
                    ("invalid_source_content" if isinstance(exc, (ValueError, UnicodeError))
                     else "network_or_timeout"))
            if isinstance(exc, HTTPError):
                exc.close()
            result["source_failures"].append({"url": item["url"], "host": item["host"],
                                               "code": code})
    if goals.get(goal_id)["status"] != "active" or not goals.get(goal_id)["public_research_allowed"]:
        result.update(status="research_permission_revoked", reason="owner_goal_changed")
        return result
    if not result["documents"]:
        result.update(status="insufficient_readable_sources", reason="all_source_reads_failed")
        return result
    decision = assess_general_documents(focus, result["documents"], trusted_hosts=trusted)
    result.update(status=decision["status"], reason=decision["reason"],
                  contradictions=decision["contradictions"],
                  unmatched_statements=decision["unmatched_statements"],
                  freshness=decision["freshness"], confidence=decision["confidence"],
                  verified_at=decision["verified_at"])
    directory = root / "memory/learning_goal_documents"
    if directory.parent.is_symlink() or directory.is_symlink():
        raise ValueError("Learning document directory is unsafe")
    artifact = directory / f"{goal_id}_{uuid4().hex}.json"
    saved = {"schema": "sira.learning_goal_general_documents.v1",
             "learning_goal_id": goal_id, "topic": goal["topic"],
             "research_query": focus,
             "status": "quotes_need_claim_verification" if decision["status"] == "corroborated"
                       else decision["status"],
             "documents": result["documents"], "metadata_evidence": [],
             "source_failures": result["source_failures"],
             "contradictions": result["contradictions"],
             "unmatched_statements": result["unmatched_statements"],
             "verified_claims_recorded": False}
    if artifact.is_symlink():
        raise ValueError("Learning document artifact is unsafe")
    write_json(artifact, saved)
    raw = artifact.read_bytes()
    if len(raw) > 1_000_000:
        raise ValueError("Learning evidence artifact exceeds bound")
    artifact_sha = hashlib.sha256(raw).hexdigest()
    result["artifact"] = str(artifact)
    result["artifact_sha256"] = artifact_sha
    if decision["status"] != "corroborated":
        return result
    first, second = decision["support"]
    pair = [(row["url"], row["content_sha256"]) for row in (first, second)]
    evidence_sha = hashlib.sha256(json.dumps(pair, ensure_ascii=False,
                                              separators=(",", ":")).encode()).hexdigest()
    claim = decision["claim"]
    key = auto_key(claim)
    store = KnowledgeConsolidationStore(root)
    for index, row in enumerate((first, second), start=1):
        latest = goals.get(goal_id)
        if (latest["status"] != "active" or latest["public_research_allowed"] is not True
                or latest["topic"] != goal["topic"]
                or hashlib.sha256(artifact.read_bytes()).hexdigest() != artifact_sha):
            result.update(status="research_permission_revoked", reason="evidence_or_owner_changed")
            return result
        store.record_evidence(
            key, claim, evidence_id=stable_evidence_id(evidence_sha, claim, row["url"]),
            source_id=f"S{index}", source_url=row["url"], confidence=.85,
            verifier_kind="general_exact_sentence_two_hosts_v1",
            evidence_sha256=evidence_sha, retrieved_at=row["retrieved_at"], verified=True)
    latest = goals.get(goal_id)
    if (latest["status"] != "active" or not latest["public_research_allowed"]
            or hashlib.sha256(artifact.read_bytes()).hexdigest() != artifact_sha):
        result.update(status="research_permission_revoked", reason="evidence_or_owner_changed")
        return result
    consolidated = store.consolidate(key, stale_after_days=_STALE_AFTER_DAYS)
    if consolidated.status != "consolidated" or consolidated.host_count < 2:
        result.update(status="insufficient_corroboration", reason=consolidated.reason)
        return result
    result.update(status="verified_knowledge_recorded", reason="independent_exact_text",
                  verified_claim_count=1,
                  claims=[{"claim": claim, "knowledge_key": key,
                           "source_urls": [first["url"], second["url"]],
                           "evidence_sha256": evidence_sha,
                           "artifact_sha256": artifact_sha}])
    return result
