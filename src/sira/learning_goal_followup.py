"""Use independently hosted public titles as unverified search hints."""
from __future__ import annotations

from typing import Mapping
import unicodedata
from urllib.parse import urlsplit


_HOSTS = frozenset({"en.wikipedia.org", "doaj.org", "arxiv.org"})
_COMMON = frozenset({
    "about", "against", "and", "article", "from", "into", "more", "overview",
    "paper", "review", "study", "the", "using", "with", "ignore", "instructions",
    "prompt", "previous", "system",
})
_INCOMPLETE = frozenset({
    "sources_discovered_needs_verification", "independent_source_unavailable",
    "research_sources_insufficient", "research_failed",
})


def _words(value: str) -> list[str]:
    words: list[str] = []
    current: list[str] = []
    for char in value.casefold():
        if unicodedata.category(char)[0] in {"L", "M", "N"}:
            current.append(char)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return words


def _terms(value: str) -> set[str]:
    return {word for word in _words(value) if len(word) >= 3 and word not in _COMMON}


def valid_hint(focus: str, hint: object) -> bool:
    if not isinstance(hint, dict) or set(hint) != {"term", "source_hosts", "source_urls"}:
        return False
    term, hosts, urls = hint["term"], hint["source_hosts"], hint["source_urls"]
    if (not isinstance(term, str) or not 3 <= len(term) <= 24
            or _words(term) != [term] or term in _terms(focus)
            or term in _COMMON or not all(unicodedata.category(c)[0] in {"L", "M"} for c in term)
            or not isinstance(hosts, list) or len(hosts) != 2
            or not all(isinstance(host, str) for host in hosts)
            or hosts != sorted(set(hosts)) or not set(hosts) <= _HOSTS
            or not isinstance(urls, list) or len(urls) != 2):
        return False
    for host, url in zip(hosts, urls):
        if not isinstance(url, str) or len(url) > 2048:
            return False
        try:
            parsed = urlsplit(url)
        except ValueError:
            return False
        if (parsed.scheme != "https" or parsed.hostname != host
                or parsed.username or parsed.password):
            return False
    return True


def title_followup_questions(focus: str, research: Mapping[str, object] | None) -> list[dict]:
    """Propose at most three unverified searches grounded in two source titles."""
    if not isinstance(focus, str) or not 1 <= len(focus) <= 500 or not isinstance(research, Mapping):
        return []
    anchors = _terms(focus)
    if not anchors:
        return []
    required = min(2, len(anchors))
    possible: dict[str, dict[str, tuple[str, int]]] = {}
    for provider_kind in ("knowledge", "open_access"):
        batch = research.get(provider_kind)
        rows = batch.get("results") if isinstance(batch, Mapping) else None
        if not isinstance(rows, list):
            continue
        for row in rows[:6]:
            if not isinstance(row, Mapping):
                continue
            title, url = row.get("title"), row.get("url")
            if (not isinstance(title, str) or not 1 <= len(title) <= 240
                    or not isinstance(url, str) or len(url) > 2048):
                continue
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            host = parsed.hostname
            if (parsed.scheme != "https" or host not in _HOSTS
                    or parsed.username or parsed.password):
                continue
            title_terms = _terms(title)
            matched = len(anchors & title_terms)
            if matched < required:
                continue
            for term in sorted(title_terms - anchors):
                if (3 <= len(term) <= 24
                        and all(unicodedata.category(c)[0] in {"L", "M"} for c in term)):
                    sources = possible.setdefault(term, {})
                    if host not in sources or matched > sources[host][1]:
                        sources[host] = (url, matched)
    questions = []
    for term, sources in sorted(possible.items(), key=lambda item: (
            -len(item[1]), -min(score for _, score in item[1].values()),
            -sum(score for _, score in item[1].values()), item[0])):
        if len(sources) >= 2:
            hosts = sorted(sources, key=lambda host: (-sources[host][1], host))[:2]
            hosts.sort()
            hint = {"term": term, "source_hosts": hosts,
                    "source_urls": [sources[host][0] for host in hosts]}
            if valid_hint(focus, hint):
                questions.append(hint)
                if len(questions) == 3:
                    break
    return questions


def title_followup_hint(focus: str, research: Mapping[str, object] | None) -> dict | None:
    """Retain the earlier single-hint interface for stored goals."""
    questions = title_followup_questions(focus, research)
    return questions[0] if questions else None


def pending_question(focus: str, row: object) -> dict | None:
    if not isinstance(row, dict) or row.get("last_outcome") not in _INCOMPLETE:
        return None
    questions = row.get("questions")
    if isinstance(questions, list):
        for question in questions:
            if (isinstance(question, dict) and question.get("attempted") is False
                    and valid_hint(focus, {key: question.get(key) for key in
                                           ("term", "source_hosts", "source_urls")})):
                return question
        return None
    # Legacy goals have only one hint and alternate it with the owner focus.
    if (type(row.get("attempt_count")) is int and row["attempt_count"] % 2
            and valid_hint(focus, row.get("hint"))):
        return row["hint"]
    return None


def search_query_for_focus(goal: Mapping[str, object], focus: str) -> tuple[str, str]:
    """Alternate baseline and hinted queries while preserving owner focus."""
    if goal.get("public_research_allowed") is not True:
        return focus, "owner_focus"
    followups = goal.get("research_followups")
    row = followups.get(focus) if isinstance(followups, dict) else None
    hint = pending_question(focus, row)
    if hint is None:
        return focus, "owner_focus"
    query = focus + " " + hint["term"]
    if len(query) > 500:
        return focus, "owner_focus"
    return query, "unverified_title_followup"
