"""Conservative public-document learning for arbitrary owner topics.

Only an exact, topic-relevant statement in documents from two separate
provider hosts may enter verified knowledge. Other text stays source evidence.
"""
from __future__ import annotations

import hashlib
import json
from itertools import combinations
from pathlib import Path
import re
from typing import Any, Callable, Mapping
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request
from uuid import uuid4

from .knowledge_consolidation import KnowledgeConsolidationStore, auto_key, stable_evidence_id
from .learning_goal_official_sources import (
    _MAX_PDF_BYTES, _extract_public_document_text,
)
from .learning_goal_source_review import review_discovered_sources
from .learning_goals import LearningGoalStore
from .models import utc_now
from .providers.http_json import open_request
from .reading import public_url
from .storage import write_json


_PUBLIC_PROVIDER_HOSTS = frozenset({
    "en.wikipedia.org", "doaj.org", "arxiv.org",
    "www.cisa.gov", "cheatsheetseries.owasp.org",
})
_MAX_HTML_BYTES = 1024 * 1024
_MAX_TEXT_CHARS = 80_000
_MAX_DOCUMENTS = 2
_MAX_CANDIDATES = 3
_MAX_CLAIMS = 3
_MAX_PRIOR_ARTIFACTS = 32

# Curated official pages for this owner study focus. Their text is only
# source evidence; independent claim verification still applies below.
_OFFICIAL_FOCUS_PAGES = {
    "cybersecurity risk assessment and threat modeling": (
        ("https://www.cisa.gov/resources-tools/resources/risk-assessment-methodologies",
         "Risk Assessment Methodologies"),
        ("https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html",
         "Threat Modeling Cheat Sheet"),
    ),
    "penetration testing scope and authorization": (
        ("https://cheatsheetseries.owasp.org/cheatsheets/Vulnerability_Disclosure_Cheat_Sheet.html",
         "Vulnerability Disclosure Cheat Sheet"),
        ("https://www.cisa.gov/news-events/news/"
         "cisa-issues-final-vulnerability-disclosure-policy-directive-federal-agencies",
         "CISA Vulnerability Disclosure Policy Directive"),
    ),
    "responsible vulnerability disclosure": (
        ("https://cheatsheetseries.owasp.org/cheatsheets/Vulnerability_Disclosure_Cheat_Sheet.html",
         "Vulnerability Disclosure Cheat Sheet"),
        ("https://www.cisa.gov/news-events/news/"
         "cisa-issues-final-vulnerability-disclosure-policy-directive-federal-agencies",
         "CISA Vulnerability Disclosure Policy Directive"),
    ),
}


_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 2


def _same_host_redirect(current: str, error: HTTPError, origin_host: str) -> str | None:
    """The redirect target only when it stays on the same host over plain https."""
    location = error.headers.get("Location") if error.headers else None
    if error.code not in _REDIRECT_CODES or not isinstance(location, str) or len(location) > 2048:
        return None
    target = urljoin(current, location.strip())
    parts = urlsplit(target)
    if (parts.scheme != "https" or parts.hostname != origin_host or parts.username
            or parts.password or parts.port not in (None, 443) or parts.fragment):
        return None
    return target


def _fetch_public_document(url: str) -> tuple[str, str, bytes]:
    """GET a bounded HTML or PDF document with same-host HTTPS redirects only."""
    origin_host = urlsplit(url).hostname
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        request = Request(current, method="GET", headers={
            "Accept": "text/html, application/pdf", "User-Agent": "SIRA/1.8 public research",
        })
        try:
            with open_request(request, 10) as response:
                content_type = response.headers.get_content_type()
                limit = (_MAX_PDF_BYTES if content_type == "application/pdf"
                         or urlsplit(response.geturl()).path.lower().endswith(".pdf")
                         else _MAX_HTML_BYTES)
                # The requested URL is returned: the redirect never left the host.
                return (url if current != url else response.geturl(),
                        content_type, response.read(limit + 1))
        except HTTPError as error:
            target = _same_host_redirect(current, error, origin_host)
            error.close() if target else None
            if target is None or _hop == _MAX_REDIRECTS:
                raise
            current = target
    raise ValueError("redirect_limit")


def _goal_allowed(store: LearningGoalStore, goal_id: str, topic: str,
                  focus: str | None = None) -> bool:
    current = store.get(goal_id)
    return (current["topic"] == topic and current["status"] == "active"
            and current["public_research_allowed"] is True
            and (focus is None or focus == topic or focus in current.get("study_plan", [])))


def _topic_terms(topic: str) -> set[str]:
    return {word for word in _words(topic)
            if len(word) > 2 and word not in {"about", "and", "for", "learn", "the", "with"}}


def _words(value: str) -> list[str]:
    # Include combining marks so a word in Bangla or another script is not
    # split into unrelated fragments by a Latin-centric regex.
    tokens, current = [], []
    for char in value.casefold():
        if unicodedata.category(char)[0] in {"L", "M", "N"}:
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _statements(text: str, topic: str) -> dict[str, str]:
    terms = _topic_terms(topic)
    required = min(2, len(terms))
    if not required:
        return {}
    candidates = {}
    # Restrict to complete, short sentences and keep the exact text as the
    # claim. A title, summary, or model paraphrase is never enough.
    for match in re.finditer(r"[^.!?।؟。\n]{40,360}[.!?।؟。](?=\s|$)", text):
        sentence = " ".join(match.group().split())
        if not 7 <= len(sentence.split()) <= 55:
            continue
        words = set(_words(sentence))
        if len(words & terms) < required:
            continue
        key = " ".join(sentence.casefold().split())
        candidates.setdefault(key, sentence)
        if len(candidates) >= 500:
            break
    return candidates


def verify_shared_statements(root: Path, goal_id: str, topic: str,
                             documents: list[Mapping[str, Any]], *,
                             focus: str | None = None) -> dict[str, Any]:
    """Validate exact document bytes before recording shared statements."""
    root = Path(root).resolve()
    goals = LearningGoalStore(root)
    if not _goal_allowed(goals, goal_id, topic, focus):
        raise ValueError("Learning goal public research is disabled")
    if not isinstance(documents, list) or len(documents) != _MAX_DOCUMENTS:
        raise ValueError("Two independently hosted documents are required")

    verified_documents = []
    for row in documents:
        if not isinstance(row, Mapping):
            raise ValueError("Invalid document")
        url = public_url(row.get("url"))
        host = urlsplit(url).hostname
        text = row.get("text")
        if (host not in _PUBLIC_PROVIDER_HOSTS or row.get("host") != host
                or not isinstance(text, str) or not 40 <= len(text) <= _MAX_TEXT_CHARS
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != row.get("content_sha256")
                or not isinstance(row.get("retrieved_at"), str)):
            raise ValueError("Document provenance or digest invalid")
        verified_documents.append(dict(row))
    if len({row["host"] for row in verified_documents}) != _MAX_DOCUMENTS:
        raise ValueError("Two separate provider hosts are required")

    statements = [_statements(row["text"], focus or topic) for row in verified_documents]
    shared = sorted(set(statements[0]) & set(statements[1]))[:_MAX_CLAIMS]
    claim_rows = []
    if shared:
        if not _goal_allowed(goals, goal_id, topic, focus):
            raise ValueError("Learning goal permission was revoked")
        provenance = hashlib.sha256(json.dumps(
            [(row["url"], row["content_sha256"]) for row in verified_documents],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        memory = KnowledgeConsolidationStore(root)
        for sentence in shared:
            if not _goal_allowed(goals, goal_id, topic, focus):
                raise ValueError("Learning goal permission was revoked")
            claim = statements[0][sentence]
            key = auto_key(claim)
            for index, row in enumerate(verified_documents, start=1):
                memory.record_evidence(
                    key, claim,
                    evidence_id=stable_evidence_id(provenance, claim, row["url"]),
                    source_id=f"S{index}", source_url=row["url"],
                    confidence=.85, verifier_kind="exact_sentence_two_public_documents_v1",
                    evidence_sha256=provenance, retrieved_at=row["retrieved_at"],
                    verified=True,
                )
            decision = memory.consolidate(key).to_dict()
            if decision["status"] != "consolidated" or decision["host_count"] < 2:
                raise ValueError("Source corroboration was not consolidated")
            claim_rows.append({"claim": claim, "knowledge_key": key,
                               "source_urls": [row["url"] for row in verified_documents]})
    return {"status": "verified_knowledge_recorded" if claim_rows else "unverified_source_statements",
            "verified_claim_count": len(claim_rows), "claims": claim_rows}


def _prior_source_url(root: Path, goal_id: str, topic: str, query: str,
                      seen_hosts: set[str]) -> tuple[str, str, str] | None:
    """Find a prior partial source URL; its text must be fetched again."""
    directory = root / "memory" / "learning_goal_documents"
    if directory.parent.is_symlink() or directory.is_symlink():
        raise ValueError("Learning document directory is unsafe")
    recent = []
    for path in directory.glob(f"{goal_id}_*.json"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        if 0 < stat.st_size <= 1_000_000:
            recent.append((stat.st_mtime_ns, path))
    for _, path in sorted(recent, reverse=True)[:_MAX_PRIOR_ARTIFACTS]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (not isinstance(payload, dict)
                or payload.get("schema") != "sira.learning_goal_general_documents.v1"
                or payload.get("learning_goal_id") != goal_id
                or payload.get("topic") != topic
                or payload.get("research_query") != query
                or payload.get("verified_claims_recorded") is not False):
            continue
        documents = payload.get("documents")
        if not isinstance(documents, list) or len(documents) != 1 or not isinstance(documents[0], dict):
            continue
        row = documents[0]
        try:
            url = public_url(row.get("url"))
            host = urlsplit(url).hostname
            text = row.get("text")
            if (len(url) > 2048 or urlsplit(url).scheme != "https" or host not in _PUBLIC_PROVIDER_HOSTS
                    or row.get("host") != host or host in seen_hosts
                    or (host == "arxiv.org" and not re.fullmatch(
                        r"/abs/[A-Za-z0-9.\-/]{1,100}", urlsplit(url).path))
                    or not isinstance(text, str) or not 40 <= len(text) <= _MAX_TEXT_CHARS
                    or hashlib.sha256(text.encode("utf-8")).hexdigest() != row.get("content_sha256")
                    or not isinstance(row.get("retrieved_at"), str)
                    or row.get("verified") is not False
                    or not isinstance(row.get("title"), str)):
                continue
        except (ValueError, TypeError, UnicodeError):
            continue
        return url, row["title"][:240], host
    return None


def collect_and_verify_goal_sources(
    root: Path, goal_id: str, topic: str, research: Mapping[str, Any], *,
    fetcher: Callable[[str], tuple[str, str, bytes]] = _fetch_public_document,
    focus: str | None = None,
) -> dict[str, Any]:
    """Read up to three public pages, verifying only an independently hosted pair."""
    root = Path(root).resolve()
    goals = LearningGoalStore(root)
    if not _goal_allowed(goals, goal_id, topic, focus):
        raise ValueError("Learning goal public research is disabled")
    review = review_discovered_sources(focus or topic, research)
    selected = []
    seen_hosts = set()
    official_pages = _OFFICIAL_FOCUS_PAGES.get(" ".join((focus or topic).casefold().split()), ())
    for url, title in official_pages:
        host = urlsplit(url).hostname
        selected.append((url, title, host, {}))
        seen_hosts.add(host)
    for candidate in review["candidates"]:
        if candidate["overlap"] not in {"high", "partial"}:
            continue
        if official_pages and (candidate["source_host"] != "arxiv.org"
                               or len(candidate["matched_title_terms"]) < 2):
            continue
        row = research[candidate["provider_kind"]]["results"][candidate["result_index"]]
        try:
            url = public_url(row["url"])
            host = urlsplit(url).hostname
        except (KeyError, ValueError, TypeError, UnicodeError):
            continue
        if (urlsplit(url).scheme != "https" or host not in _PUBLIC_PROVIDER_HOSTS
                or host in seen_hosts):
            continue
        if host == "arxiv.org" and not re.fullmatch(r"/abs/[A-Za-z0-9.\-/]{1,100}", urlsplit(url).path):
            continue
        selected.append((url, candidate["title"], host, row))
        seen_hosts.add(host)
        if len(selected) == _MAX_CANDIDATES:
            break

    if not official_pages and selected and len(selected) < _MAX_CANDIDATES:
        prior = _prior_source_url(root, goal_id, topic, focus or topic, seen_hosts)
        if prior is not None:
            url, title, host = prior
            selected.append((url, title, host, {}))

    result: dict[str, Any] = {"status": "insufficient_independent_sources",
                              "verified_claim_count": 0, "claims": [],
                              "api_requests": 0, "metered_model_requests": 0,
                              "promotion_performed": False, "documents": [],
                              "source_failures": [], "metadata_evidence": []}
    if not selected:
        return result

    max_documents = _MAX_DOCUMENTS if official_pages else _MAX_CANDIDATES
    for url, title, host, row in selected:
        if not _goal_allowed(goals, goal_id, topic, focus):
            result["status"] = "research_permission_revoked"
            return result
        result["api_requests"] += 1
        try:
            actual, content_type, raw = fetcher(url)
            if (actual != url or not isinstance(raw, bytes) or not raw
                    or len(raw) > (_MAX_PDF_BYTES if content_type == "application/pdf"
                                   or raw.startswith(b"%PDF-") else _MAX_HTML_BYTES)):
                raise ValueError("Unexpected public document response")
            content = _extract_public_document_text(raw, content_type)
            result["documents"].append({
                "url": url, "host": host, "title": title, "text": content,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "retrieved_at": utc_now(), "verified": False,
            })
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeError) as exc:
            code = (f"http_{exc.code}" if isinstance(exc, HTTPError) else
                    getattr(exc, "code", None) or
                    ("invalid_source_content" if isinstance(exc, (ValueError, UnicodeError))
                     else "network_or_timeout"))
            if isinstance(exc, HTTPError):
                exc.close()
            result["source_failures"].append({"host": host, "url": url, "code": code})
            result["status"] = "independent_source_unavailable"
            # Search metadata is useful for the next research step, but is
            # never passed to the document claim verifier.
            abstract = row.get("abstract") if host == "doaj.org" and row.get("provider") == "doaj" else None
            if isinstance(abstract, str) and 40 <= len(abstract) <= 10_000:
                try:
                    abstract_digest = hashlib.sha256(abstract.encode("utf-8")).hexdigest()
                except UnicodeError:
                    pass
                else:
                    result["metadata_evidence"].append({
                        "url": url, "host": host, "title": title,
                        "abstract": abstract, "abstract_sha256": abstract_digest,
                        "retrieved_at": row.get("retrieved_at"), "verified": False,
                    })
            continue
        if len(result["documents"]) == _MAX_DOCUMENTS and (
                official_pages or len(selected) <= _MAX_DOCUMENTS or
                set(_statements(result["documents"][0]["text"], focus or topic)) &
                set(_statements(result["documents"][1]["text"], focus or topic))):
            break
        if len(result["documents"]) == max_documents:
            break
    if not _goal_allowed(goals, goal_id, topic, focus):
        result["status"] = "research_permission_revoked"
        return result
    directory = root / "memory" / "learning_goal_documents"
    if directory.parent.is_symlink() or directory.is_symlink():
        raise ValueError("Learning document directory is unsafe")
    artifact = directory / f"{goal_id}_{uuid4().hex}.json"
    if artifact.is_symlink():
        raise ValueError("Learning document artifact is unsafe")
    if result["documents"] or result["metadata_evidence"]:
        # Preserve available source text, including a partial read, before
        # any knowledge row is written. Abstract metadata stays unverified.
        write_json(artifact, {"schema": "sira.learning_goal_general_documents.v1",
                              "learning_goal_id": goal_id, "topic": topic,
                              "research_query": focus or topic,
                              "status": (result["status"] if len(result["documents"]) < _MAX_DOCUMENTS
                                         else "quotes_need_claim_verification"),
                              "documents": result["documents"],
                              "metadata_evidence": result["metadata_evidence"],
                              "source_failures": result["source_failures"],
                              "verified_claims_recorded": False})
        result["artifact"] = str(artifact)
    if len(result["documents"]) >= _MAX_DOCUMENTS:
        decision = {"status": "unverified_source_statements", "verified_claim_count": 0, "claims": []}
        for pair in combinations(result["documents"], _MAX_DOCUMENTS):
            decision = verify_shared_statements(root, goal_id, topic, list(pair), focus=focus)
            if decision["verified_claim_count"]:
                break
        result.update(decision)
        if decision["verified_claim_count"] == 0 and official_pages and focus is not None:
            from .learning_goal_curated_runtime import verify_curated_saved_claim
            result.update(verify_curated_saved_claim(root, goal_id, topic, focus, artifact))
        if result["status"] == "unverified_source_statements" and result["source_failures"]:
            result["status"] = "independent_source_unavailable"
    return result
