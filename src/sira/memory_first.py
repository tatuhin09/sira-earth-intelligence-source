"""A79 read-only, bounded decision to reuse current verified knowledge.

This layer never searches the network or promotes knowledge. A single claim is
reported as one narrow fact, never as topic mastery or an active skill.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import unicodedata

from .knowledge_consolidation import KnowledgeConsolidationStore, MIN_CONFIDENCE


_IGNORE = frozenset({"a", "an", "and", "are", "about", "can", "do", "explain", "for",
                     "from", "give", "how", "in", "is", "me", "of", "on", "or", "please",
                     "the", "to", "what", "with", "you"})
_NEGATIONS = frozenset({"no", "not", "never"})
_MAX_CANDIDATES = 12
_MAX_RESULTS = 3
_MAX_CONTEXT_CHARS = 2400


def _terms(text: str) -> set[str]:
    terms = set(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())) - _IGNORE
    return {"test" if term in {"tests", "testing"} else term for term in terms}


def _polarity(text: str) -> tuple[str, bool]:
    tokens = re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())
    return " ".join(token for token in tokens if token not in _NEGATIONS), any(
        token in _NEGATIONS for token in tokens)


def _needs_current_research(query: str) -> bool:
    lower = query.casefold()
    return (bool(re.search(r"\b(latest|today|recently|up.to.date)\b", lower))
            or "as of " in lower or "current research" in lower)


def resolve_memory_first(root: Path, question: str, *,
                         request_fresh_research: bool = False,
                         now: str | None = None, limit: int = 2) -> dict:
    """Return one narrow memory answer or an explicit reason for research.

    All outputs are local decisions: a research-needed result is not a network
    request. Caller policy must separately authorize any subsequent research.
    """
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 500:
        raise ValueError("Question must contain 1..500 characters")
    if type(request_fresh_research) is not bool or type(limit) is not int or not 1 <= limit <= _MAX_RESULTS:
        raise ValueError("Invalid memory-first request")
    root = Path(root).resolve()
    question = " ".join(question.split())
    terms = _terms(question) - {"latest", "today", "recently", "current", "research", "fresh"}
    fresh_request = request_fresh_research or _needs_current_research(question)
    decision = {
        "schema": "sira.memory_first_resolution.v1", "question": question,
        "status": "research_needed", "reason": "no_relevant_verified_memory",
        "scope": "one_verified_claim", "results": [], "fresh_research_requested": fresh_request,
        "research_required": True, "api_requests": 0, "metered_model_requests": 0,
        "paid_spending": False, "skill_activated": False,
    }
    if not terms:
        decision["reason"] = "insufficient_question_terms"
        return decision
    db = root / "memory" / "sira_knowledge.sqlite3"
    if db.is_symlink():
        decision["reason"] = "unsafe_memory_path"
        return decision
    if not db.is_file():
        decision["reason"] = "no_verified_memory"
        return decision
    store = KnowledgeConsolidationStore(root)
    rows = store.search(" ".join(sorted(terms)), limit=_MAX_CANDIDATES,
                        include_stale=True, include_conflicted=True, now=now)
    relevant = []
    for row in rows:
        matched = terms & _terms(str(row["claim_text"]))
        if len(matched) < min(2, len(terms)) or len(matched) / len(terms) < .8:
            continue
        relevant.append(row)
    if not relevant:
        return decision
    if any(row["status"] == "conflicted" or row["freshness"] == "conflicted"
           for row in relevant):
        decision["reason"] = "contradicted_memory"
        return decision
    if any(row["freshness"] == "invalid_lifecycle_metadata" for row in relevant):
        decision["reason"] = "invalid_lifecycle_metadata"
        return decision
    fresh = [row for row in relevant if row["freshness"] == "fresh" and row["status"] == "active"]
    polarity = {_polarity(str(row["claim_text"])) for row in fresh}
    if any((base, not negative) in polarity for base, negative in polarity):
        decision["reason"] = "contradictory_verified_claims"
        return decision
    if fresh_request:
        decision["reason"] = "explicit_fresh_research_requested"
        # Bounded memory context remains available to the caller, but it is not
        # accepted as a current answer to an explicit freshness request.
        decision["local_knowledge_keys"] = [str(row["knowledge_key"]) for row in fresh[:limit]]
        return decision
    if not fresh:
        decision["reason"] = ("stale_memory_requires_revalidation"
                              if any(row["freshness"] == "stale" for row in relevant)
                              else "superseded_or_deprecated_memory")
        return decision
    strong = [row for row in fresh if (float(row["confidence"]) >= MIN_CONFIDENCE
             and int(row["evidence_count"]) >= 2 and int(row["source_count"]) >= 2
             and int(row["host_count"]) >= 2)]
    if not strong:
        decision["reason"] = "insufficient_evidence_strength"
        return decision
    results = []
    current_time = (datetime.now(timezone.utc) if now is None else
                    datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc))
    stale_independent = False
    for row in strong[:limit]:
        sources = store.verified_source_evidence(str(row["knowledge_key"]), limit=4, now=now)
        if len({source["source_host"] for source in sources}) < 2:
            continue
        fresh_sources = []
        for source in sources:
            try:
                retrieved = datetime.fromisoformat(str(source["retrieved_at"]).replace("Z", "+00:00"))
                age = (current_time - retrieved.astimezone(timezone.utc)).total_seconds()
            except (ValueError, TypeError):
                continue
            if 0 <= age <= int(row["effective_stale_after_days"]) * 86400:
                fresh_sources.append(source)
        if len({source["source_host"] for source in fresh_sources}) < 2:
            stale_independent = True
            continue
        sources = fresh_sources
        # A text/URL budget is a hard bound; never truncate a source reference
        # into a plausible but incorrect citation.
        claim = str(row["claim_text"])
        if len(claim) > 500 or any(len(str(source["source_url"])) > 512 for source in sources):
            continue
        results.append({
            "knowledge_key": str(row["knowledge_key"]), "claim": claim,
            "confidence": float(row["confidence"]), "evidence_count": int(row["evidence_count"]),
            "host_count": int(row["host_count"]), "freshness": "fresh",
            "last_verified_at": str(row["last_verified_at"]),
            "stale_after_days": int(row["effective_stale_after_days"]),
            "freshness_class": str(row["freshness_class"]),
            "freshness_reason": str(row["freshness_reason"]),
            "reconsider_after": str(row["reconsider_after"]),
            "retrieval_score": float(row["retrieval_score"]),
            "provenance": [{key: source[key] for key in (
                "evidence_id", "source_id", "source_url", "source_host",
                "evidence_sha256", "verifier_kind", "retrieved_at")}
                for source in sources],
        })
    if not results:
        decision["reason"] = ("stale_independent_provenance" if stale_independent
                              else "verified_provenance_unavailable")
        return decision
    if sum(len(row["claim"]) + sum(len(p["source_url"]) for p in row["provenance"])
           for row in results) > _MAX_CONTEXT_CHARS:
        decision["reason"] = "context_budget_exceeded"
        return decision
    decision.update(status="memory_resolved", reason="fresh_independent_verified_claim",
                    research_required=False, results=results)
    return decision
