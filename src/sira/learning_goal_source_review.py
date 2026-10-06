"""Offline triage of learning-goal search metadata; no claim verification."""
from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit


_COMMON = frozenset({"about", "and", "for", "from", "into", "the", "using", "with"})


def _terms(value: str) -> set[str]:
    return {
        term for term in re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)
        if len(term) >= 3 and term not in _COMMON
    }


def review_discovered_sources(topic: str, research: Mapping[str, Any] | None) -> dict[str, Any]:
    """Rank source metadata for further review without interpreting its claims.

    An overlap is only a lexical hint; all search results remain unverified.
    Multiple records from the same search host do not prove independence.
    """
    if not isinstance(topic, str) or not 1 <= len(topic.strip()) <= 500:
        raise ValueError("A bounded learning topic is required")
    if research is not None and not isinstance(research, Mapping):
        raise ValueError("Research must be a mapping")
    terms = _terms(topic)
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for provider_kind in ("knowledge", "open_access"):
        batch = (research or {}).get(provider_kind)
        if not isinstance(batch, Mapping) or not isinstance(batch.get("results"), list):
            continue
        # Two free paper providers can each return up to three results.
        for result_index, row in enumerate(batch["results"][:6]):
            if not isinstance(row, Mapping):
                continue
            title = row.get("title")
            url = row.get("url")
            if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or len(url) > 2048:
                continue
            try:
                parsed = urlsplit(url)
                host = parsed.hostname
            except ValueError:
                continue
            if parsed.scheme != "https" or not host or parsed.username or parsed.password:
                continue
            normalized = parsed._replace(fragment="").geturl().casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            title_terms = terms & _terms(title)
            description = row.get("description") or row.get("abstract")
            excerpt_terms = terms & _terms(description[:4000]) if isinstance(description, str) else set()
            covered = title_terms | excerpt_terms
            overlap = ("high" if terms and title_terms == terms else
                       "partial" if title_terms else
                       "excerpt_only" if excerpt_terms else "none")
            candidates.append({
                "title": " ".join(title.split())[:240],
                "source_host": host.casefold(),
                "provider_kind": provider_kind,
                "result_index": result_index,
                "overlap": overlap,
                "matched_topic_terms": sorted(covered),
                "matched_title_terms": sorted(title_terms),
                "metadata_excerpt_available": bool(isinstance(description, str) and description.strip()),
                "claims_verified": False,
            })
    priority = {"high": 0, "partial": 1, "excerpt_only": 2, "none": 3}
    candidates.sort(key=lambda item: (
        priority[item["overlap"]], -len(item["matched_title_terms"]),
        -len(item["matched_topic_terms"]),
        item["title"].casefold(), item["source_host"],
    ))
    return {
        "schema": "sira.learning_goal_source_review.v1",
        "status": "needs_claim_verification",
        "source_count": len(candidates),
        "distinct_host_count": len({item["source_host"] for item in candidates}),
        "high_overlap_count": sum(item["overlap"] == "high" for item in candidates),
        "high_overlap_host_count": len({
            item["source_host"] for item in candidates if item["overlap"] == "high"
        }),
        "candidates": candidates,
        "verified_claims_recorded": False,
        "model_requests": 0,
        "network_requests": 0,
    }
