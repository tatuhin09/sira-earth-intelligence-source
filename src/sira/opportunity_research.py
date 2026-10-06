"""Free/public external research enrichment for evidence-backed opportunities.

v1.0C-3c performs bounded scholarly metadata research using zero-paid providers.
It never invokes a metered model and never edits the source tree.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Iterable
from typing import Mapping
from uuid import uuid4

from .config import Settings
from .memory import MemoryStore, Observation
from .knowledge_runtime import knowledge_context_for_query
from .models import ProviderError, utc_now
from .papers import Paper, PaperProvider
from .providers.arxiv import ArxivProvider
from .providers.crossref import CrossrefProvider
from .providers.openalex import OpenAlexProvider
from .self_modification import PROTECTED_PATHS
from .storage import Cache, write_json
from .strategy_learner import StrategyLearner

RESEARCH_POLICY_VERSION = 4
EVIDENCE_ID_RE = re.compile(r"oe_[0-9a-f]{32}\Z")
MAX_EVIDENCE_ARTIFACTS = 200
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024
MAX_CITATIONS = 6
MAX_ABSTRACT_EXCERPT = 800
CACHE_TTL_SECONDS = 86400
PROVIDER_COOLDOWN_429_SECONDS = 900
PROVIDER_COOLDOWN_TRANSIENT_SECONDS = 120
MAX_PROVIDER_COOLDOWN_SECONDS = 3600
TRANSIENT_PROVIDER_CODES = frozenset({"http_429", "http_502", "http_503", "http_504", "network_or_timeout"})


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _load_evidence(root: Path, evidence_id: str) -> dict[str, Any]:
    if not isinstance(evidence_id, str) or not EVIDENCE_ID_RE.fullmatch(evidence_id):
        raise ValueError("Invalid opportunity evidence ID")
    base = root / "improvements" / "opportunities" / "evidence"
    if not base.is_dir():
        raise ValueError("Opportunity evidence not found; run improve evidence first")
    direct = base / f"{evidence_id}.json"
    report = _safe_json(direct)
    if report is not None and report.get("kind") == "opportunity_evidence_brief":
        return report
    paths = sorted(base.glob("oe_*.json"), key=lambda p: p.stat().st_mtime_ns if p.exists() else 0, reverse=True)
    for path in paths[:MAX_EVIDENCE_ARTIFACTS]:
        report = _safe_json(path)
        if report is not None and report.get("kind") == "opportunity_evidence_brief" and report.get("evidence_id") == evidence_id:
            return report
    raise ValueError("Opportunity evidence not found; run improve evidence first")


def _validate_current_target(root: Path, evidence: dict[str, Any]) -> None:
    assessment = evidence.get("assessment") if isinstance(evidence.get("assessment"), dict) else {}
    if assessment.get("decision") != "research_ready":
        raise ValueError("Opportunity evidence is not research-ready")
    opportunity = evidence.get("opportunity") if isinstance(evidence.get("opportunity"), dict) else {}
    rel = opportunity.get("path")
    expected = opportunity.get("source_sha256")
    if not isinstance(rel, str) or rel in PROTECTED_PATHS or not rel.startswith("src/sira/"):
        raise ValueError("Opportunity evidence target is not modifiable")
    path = (root / rel).resolve()
    public_root = (root / "src" / "sira").resolve()
    try:
        path.relative_to(public_root)
    except ValueError as exc:
        raise ValueError("Opportunity evidence target escapes modifiable source") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError("Opportunity evidence target source is unavailable")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Opportunity evidence target source is too large")
    payload = path.read_bytes()
    try:
        payload.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Opportunity evidence target is not UTF-8 text") from exc
    if not isinstance(expected, str) or _sha256_bytes(payload) != expected:
        raise ValueError("Opportunity evidence is stale because target source changed")


def _research_query(evidence: dict[str, Any]) -> str:
    opportunity = evidence.get("opportunity") if isinstance(evidence.get("opportunity"), dict) else {}
    kind = opportunity.get("type")
    structural = evidence.get("success_criteria", {}).get("structural_goal", {}) if isinstance(evidence.get("success_criteria"), dict) else {}
    metric = structural.get("metric") if isinstance(structural, dict) else None
    phrases = {
        "complex_function": "software engineering refactoring complex function control flow maintainability behavior preservation",
        "large_module": "software engineering modularization large module refactoring maintainability behavior preservation",
        "explicit_debt_marker": "software engineering technical debt refactoring maintainability regression testing",
        "syntax_parse_failure": "software engineering syntax repair parser error regression testing maintainability",
        "missing_test_reference": "software engineering test coverage regression testing characterization tests behavior preservation",
        "weak_error_handling": "software engineering exception handling error handling broad exception maintainability reliability",
        "duplicate_function_body": "software engineering code duplication duplicate code refactoring maintainability behavior preservation",
    }
    query = phrases.get(kind, "software engineering refactoring maintainability regression testing")
    if isinstance(metric, str) and metric not in {"static_signal", "parse_failures"}:
        query += " " + metric.replace("_", " ")
    return " ".join(query.split())[:500]


def _normalize_title(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", value.casefold(), re.UNICODE))


def _identity(paper: Paper) -> tuple[str, str]:
    if paper.doi:
        return ("doi", paper.doi.casefold())
    normalized = _normalize_title(paper.title)
    if normalized:
        return ("title", normalized)
    return ("url", paper.url.casefold())


def _citation(paper: Paper, citation_id: str) -> dict[str, Any]:
    abstract = paper.abstract.strip() if isinstance(paper.abstract, str) and paper.abstract.strip() else None
    if abstract is not None and len(abstract) > MAX_ABSTRACT_EXCERPT:
        abstract = abstract[: MAX_ABSTRACT_EXCERPT - 3].rstrip() + "..."
    return {
        "citation_id": citation_id,
        "source_kind": "scholarly_public_metadata",
        "provider": paper.provider,
        "providers": list(paper.providers or (paper.provider,)),
        "title": paper.title,
        "url": paper.url,
        "doi": paper.doi,
        "year": paper.year,
        "abstract_excerpt": abstract,
        "retrieved_at": paper.retrieved_at,
    }


def _merge_paper(existing: Paper, incoming: Paper) -> Paper:
    providers = tuple(dict.fromkeys((*existing.providers, *incoming.providers, existing.provider, incoming.provider)))
    abstract = existing.abstract or incoming.abstract
    authors = existing.authors or incoming.authors
    year = existing.year if existing.year is not None else incoming.year
    doi = existing.doi or incoming.doi
    pdf = existing.open_access_pdf_url or incoming.open_access_pdf_url
    published_date = existing.published_date or incoming.published_date
    return Paper(
        paper_id=existing.paper_id,
        title=existing.title,
        abstract=abstract,
        authors=authors,
        year=year,
        doi=doi,
        url=existing.url,
        open_access_pdf_url=pdf,
        retrieved_at=existing.retrieved_at,
        provider=existing.provider,
        published_date=published_date,
        providers=providers,
    )


def _dedupe(papers: Iterable[Paper]) -> list[Paper]:
    result: list[Paper] = []
    positions: dict[tuple[str, str], int] = {}
    for paper in papers:
        key = _identity(paper)
        index = positions.get(key)
        if index is not None:
            result[index] = _merge_paper(result[index], paper)
            continue
        if len(result) >= MAX_CITATIONS:
            continue
        positions[key] = len(result)
        result.append(paper)
    return result


def _strategies(evidence: dict[str, Any], citation_ids: list[str]) -> list[dict[str, Any]]:
    opportunity = evidence.get("opportunity") if isinstance(evidence.get("opportunity"), dict) else {}
    kind = opportunity.get("type")
    if kind == "complex_function":
        strategies = [
            "Extract cohesive phases into small private helpers while preserving the public function signature and output contract.",
            "Separate validation, provider/cache handling, record assembly, and final status calculation so branch-heavy responsibilities can be tested independently.",
            "Use existing characterization tests and the mapped benchmark as the behavioral oracle; do not reduce test count to satisfy the structural target.",
        ]
    elif kind == "large_module":
        strategies = [
            "Split by cohesive responsibility behind stable interfaces rather than by arbitrary line count.",
            "Move one responsibility at a time and preserve imports, tests, and benchmark behavior after each extraction.",
        ]
    elif kind == "explicit_debt_marker":
        strategies = [
            "Resolve the explicit debt marker with the smallest behavior-preserving change supported by current tests and benchmark evidence.",
        ]
    elif kind == "missing_test_reference":
        strategies = [
            "Add a focused characterization test for the public function without changing its production behavior unless the test exposes a real defect.",
            "Prefer observable input/output assertions over implementation-detail mocks, and keep the mapped benchmark green.",
        ]
    elif kind == "weak_error_handling":
        strategies = [
            "Replace silently swallowed broad exceptions with the narrowest expected exception handling that preserves the documented fallback behavior.",
            "Keep unexpected failures visible rather than converting every exception into an indistinguishable success or empty result.",
        ]
    elif kind == "duplicate_function_body":
        strategies = [
            "Remove exact duplicated behavior through a small shared helper or a single canonical implementation while preserving public call contracts.",
            "Avoid abstraction unless it reduces the measured duplicate-body signal and keeps tests and the mapped benchmark green.",
        ]
    else:
        strategies = [
            "Prefer the smallest behavior-preserving repair that satisfies the measurable success criteria and keeps all relevant tests and benchmarks green.",
        ]
    return [{"strategy": text, "basis": "local_evidence_plus_public_research", "citation_ids": citation_ids} for text in strategies]


RELEVANCE_TERMS: dict[str, tuple[tuple[str, str], ...]] = {
    "complex_function": (
        ("refactor", "refactoring"),
        ("maintainab", "maintainability"),
        ("code smell", "code_smell"),
        ("extract method", "extract_method"),
        ("method extraction", "method_extraction"),
        ("function extraction", "function_extraction"),
        ("control flow", "control_flow"),
        ("cyclomatic", "cyclomatic_complexity"),
        ("complex function", "complex_function"),
        ("complex method", "complex_method"),
        ("behavior preserv", "behavior_preservation"),
        ("behaviour preserv", "behavior_preservation"),
        ("decompos", "decomposition"),
    ),
    "large_module": (
        ("refactor", "refactoring"),
        ("maintainab", "maintainability"),
        ("modular", "modularity"),
        ("cohesion", "cohesion"),
        ("coupling", "coupling"),
        ("decompos", "decomposition"),
        ("large module", "large_module"),
    ),
    "explicit_debt_marker": (
        ("technical debt", "technical_debt"),
        ("code debt", "code_debt"),
        ("refactor", "refactoring"),
        ("maintainab", "maintainability"),
        ("debt repayment", "debt_repayment"),
    ),
    "syntax_parse_failure": (
        ("syntax repair", "syntax_repair"),
        ("parser repair", "parser_repair"),
        ("parse error", "parse_error"),
        ("error recovery", "error_recovery"),
        ("program repair", "program_repair"),
        ("regression test", "regression_testing"),
    ),
    "missing_test_reference": (
        ("test coverage", "test_coverage"),
        ("regression test", "regression_testing"),
        ("characterization test", "characterization_testing"),
        ("unit test", "unit_testing"),
        ("test adequacy", "test_adequacy"),
        ("behavior preserv", "behavior_preservation"),
        ("behaviour preserv", "behavior_preservation"),
    ),
    "weak_error_handling": (
        ("exception handling", "exception_handling"),
        ("error handling", "error_handling"),
        ("broad exception", "broad_exception"),
        ("exception smell", "exception_smell"),
        ("reliability", "reliability"),
        ("maintainab", "maintainability"),
    ),
    "duplicate_function_body": (
        ("code duplication", "code_duplication"),
        ("duplicate code", "duplicate_code"),
        ("duplicated code", "duplicate_code"),
        ("clone detection", "clone_detection"),
        ("code clone", "code_clone"),
        ("refactor", "refactoring"),
        ("maintainab", "maintainability"),
    ),
}


def _relevance(citation: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    opportunity = evidence.get("opportunity") if isinstance(evidence.get("opportunity"), dict) else {}
    kind = str(opportunity.get("type") or "")
    terms = RELEVANCE_TERMS.get(kind, RELEVANCE_TERMS["complex_function"])
    title = str(citation.get("title") or "").casefold()
    abstract = str(citation.get("abstract_excerpt") or "").casefold()
    matched: list[str] = []
    title_matches: list[str] = []
    abstract_matches: list[str] = []
    score = 0
    for needle, label in terms:
        in_title = needle in title
        in_abstract = needle in abstract
        if not in_title and not in_abstract:
            continue
        if label not in matched:
            matched.append(label)
        if in_title:
            if label not in title_matches:
                title_matches.append(label)
            score += 3
        elif in_abstract:
            if label not in abstract_matches:
                abstract_matches.append(label)
            score += 1
    eligible = bool(title_matches) or len(matched) >= 2
    return {
        "eligible": eligible,
        "score": score,
        "matched_terms": matched,
        "title_matches": title_matches,
        "abstract_matches": abstract_matches,
        "policy": "at least one target-specific title signal or two distinct target-specific signals overall",
    }


def _filter_relevant_citations(evidence: dict[str, Any], citations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    relevant: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in citations:
        relevance = _relevance(row, evidence)
        if relevance["eligible"]:
            accepted = dict(row)
            accepted["relevance"] = relevance
            relevant.append(accepted)
        else:
            rejected.append({
                "provider": row.get("provider"),
                "title": row.get("title"),
                "url": row.get("url"),
                "doi": row.get("doi"),
                "year": row.get("year"),
                "relevance": relevance,
            })
    for index, row in enumerate(relevant, start=1):
        row["citation_id"] = f"R{index}"
    return relevant, rejected


def _quality(citations: list[dict[str, Any]], failures: list[dict[str, Any]], rejected_count: int = 0) -> dict[str, Any]:
    abstract_count = sum(bool(row.get("abstract_excerpt")) for row in citations)
    providers: set[str] = set()
    for row in citations:
        raw = row.get("providers")
        if isinstance(raw, list):
            providers.update(value for value in raw if isinstance(value, str) and value)
        elif isinstance(row.get("provider"), str):
            providers.add(row["provider"])
    providers = sorted(providers)
    ready = len(citations) >= 2 and abstract_count >= 1
    return {
        "decision": "writer_ready" if ready else "insufficient_external_evidence",
        "source_count": len(citations),
        "abstract_count": abstract_count,
        "relevant_source_count": len(citations),
        "relevant_abstract_count": abstract_count,
        "rejected_irrelevant_count": rejected_count,
        "provider_count": len(providers),
        "providers": providers,
        "provider_failure_count": len(failures),
        "minimum_sources": 2,
        "minimum_abstracts": 1,
    }


def _record_provider_strategy_feedback(
    root: Path,
    *,
    provider_name: str,
    evidence_id: str,
    succeeded: bool,
    error_code: str | None = None,
) -> None:
    """Persist bounded real provider feedback for Strategy Learner.

    This stores only machine-generated strategy metadata. Raw provider error
    bodies, exception messages, credentials, query text, and citations are
    intentionally excluded.
    """
    provider_name = str(provider_name).strip()
    if not provider_name or len(provider_name) > 100:
        return
    if not isinstance(evidence_id, str) or not EVIDENCE_ID_RE.fullmatch(evidence_id):
        return

    store = MemoryStore(root)
    capability = "paper_search"
    outcome_type = "application_succeeded" if succeeded else "application_failed"
    kind = "success" if succeeded else "failure"
    category = "success" if succeeded else "provider"
    summary = (
        f"Provider strategy {provider_name} succeeded during {capability}"
        if succeeded
        else f"Provider strategy {provider_name} failed during {capability}"
    )
    safe_error_code = (
        str(error_code)[:120]
        if not succeeded and isinstance(error_code, str) and error_code
        else None
    )
    signature = (
        f"strategy-feedback:{capability}:{provider_name}:"
        f"{'success' if succeeded else 'failure'}"
    )
    digest = hashlib.sha256(
        f"{evidence_id}:{signature}".encode("utf-8")
    ).hexdigest()

    memory_id, _ = store.upsert(
        Observation(
            kind,
            category,
            capability,
            summary,
            "observed",
            provider=provider_name,
            error_code=safe_error_code,
            signature=signature,
            origin="research_outcome",
        ),
        run_id=digest[:32],
        artifact_name="provider-strategy-feedback.json",
        artifact_sha256=digest,
        outcome_status="completed" if succeeded else "failed",
    )
    store.record_outcome(
        memory_id,
        outcome_type,
        context={
            "consumer": "opportunity_research.provider_feedback",
            "strategy_id": provider_name,
            "capability": capability,
            "evidence_id": evidence_id,
        },
        weight=1.0,
    )


def _default_provider_plan(root: Path) -> tuple[tuple[PaperProvider, ...], dict[str, Any]]:
    from .capability_broker import STATUS_READY, broker_decision

    root = Path(root).resolve()
    settings = Settings(root, max_results=3)
    available: dict[str, PaperProvider] = {
        "arxiv": ArxivProvider(timeout=settings.timeout_seconds),
        "crossref": CrossrefProvider(timeout=settings.timeout_seconds),
        "openalex": OpenAlexProvider(timeout=settings.timeout_seconds),
    }

    decision = broker_decision(
        root,
        "paper_search",
        allow_metered=False,
        persist=True,
    )

    chain: list[str] = []
    selected_id = decision.get("selected_provider_id")
    if isinstance(selected_id, str) and selected_id:
        chain.append(selected_id)
    fallbacks = decision.get("fallback_provider_ids")
    if isinstance(fallbacks, list):
        chain.extend(
            value for value in fallbacks
            if isinstance(value, str) and value and value not in chain
        )

    supported_chain = [provider_id for provider_id in chain if provider_id in available]
    unsupported_chain = [provider_id for provider_id in chain if provider_id not in available]
    providers = tuple(available[provider_id] for provider_id in supported_chain)

    status = str(decision.get("status") or "provider_unavailable")
    fail_closed = status != STATUS_READY or not providers
    if fail_closed:
        providers = ()

    access_need = decision.get("access_need")
    audit = {
        "schema": "sira.opportunity_research_broker_plan.v1",
        "broker_decision_id": decision.get("decision_id"),
        "broker_artifact": decision.get("artifact"),
        "capability": "paper_search",
        "status": status,
        "consumer_provider_chain": [provider.name for provider in providers],
        "consumer_unsupported_provider_ids": unsupported_chain,
        "access_need": dict(access_need) if isinstance(access_need, Mapping) else None,
        "owner_action_required": bool(decision.get("owner_action_required", False)),
        "allow_metered": False,
        "paid_spending": False,
        "fail_closed": fail_closed,
        "provider_execution_performed": False,
        "access_request_created": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }
    return providers, audit


def _default_providers(root: Path) -> tuple[PaperProvider, ...]:
    providers, _audit = _default_provider_plan(root)
    return providers


def _cooldown_path(root: Path, provider_name: str) -> Path:
    digest = hashlib.sha256(provider_name.casefold().encode("utf-8")).hexdigest()
    return root / ".cache" / "opportunity_provider_cooldowns" / f"{digest}.json"


def _provider_cooldown(root: Path, provider_name: str, now_epoch: float) -> dict[str, Any] | None:
    value = _safe_json(_cooldown_path(root, provider_name))
    if value is None or value.get("provider") != provider_name:
        return None
    until = value.get("until_epoch")
    code = value.get("code")
    if type(until) not in (int, float) or not isinstance(code, str):
        return None
    remaining = float(until) - now_epoch
    if remaining <= 0:
        return None
    return {
        "provider": provider_name,
        "code": code,
        "remaining_seconds": round(remaining, 3),
    }


def _record_provider_cooldown(root: Path, provider_name: str, exc: ProviderError, now_epoch: float) -> None:
    if exc.code not in TRANSIENT_PROVIDER_CODES:
        return
    if type(exc.retry_after) is int and 0 < exc.retry_after <= MAX_PROVIDER_COOLDOWN_SECONDS:
        seconds = exc.retry_after
    elif exc.code == "http_429":
        seconds = PROVIDER_COOLDOWN_429_SECONDS
    else:
        seconds = PROVIDER_COOLDOWN_TRANSIENT_SECONDS
    write_json(_cooldown_path(root, provider_name), {
        "provider": provider_name,
        "code": exc.code,
        "recorded_at_epoch": now_epoch,
        "until_epoch": now_epoch + seconds,
    })


def _clear_provider_cooldown(root: Path, provider_name: str) -> None:
    path = _cooldown_path(root, provider_name)
    try:
        if path.is_file() and not path.is_symlink():
            path.unlink()
    except OSError:
        pass


def research_opportunity_free(root: Path, evidence_id: str, *, providers: tuple[PaperProvider, ...] | None = None,
                              use_cache: bool = True) -> dict[str, Any]:
    """Enrich a research-ready opportunity with bounded free/public scholarly metadata."""
    root = Path(root).resolve()
    evidence = _load_evidence(root, evidence_id)
    _validate_current_target(root, evidence)
    query = _research_query(evidence)
    local_knowledge_context = knowledge_context_for_query(root, query, limit=3)
    if providers is None:
        selected, broker_plan = _default_provider_plan(root)
    else:
        selected = tuple(providers)
        broker_plan = {
            "schema": "sira.opportunity_research_broker_plan.v1",
            "broker_decision_id": None,
            "broker_artifact": None,
            "capability": "paper_search",
            "status": "explicit_provider_override",
            "consumer_provider_chain": [
                str(getattr(provider, "name", "")) for provider in selected
            ],
            "consumer_unsupported_provider_ids": [],
            "access_need": None,
            "owner_action_required": False,
            "allow_metered": False,
            "paid_spending": False,
            "fail_closed": False,
            "provider_execution_performed": False,
            "access_request_created": False,
            "authority_granted": False,
            "promotion_authorized": False,
        }
    if not selected:
        raise ValueError(
            "Capability Broker did not provide a usable free/public research provider"
        )
    for provider in selected:
        if not getattr(provider, "name", "") or not getattr(provider, "cache_namespace", ""):
            raise ValueError("Research providers require name and cache_namespace")

    evidence_fingerprint = str(evidence.get("opportunity", {}).get("fingerprint") or evidence_id)
    cache = Cache(root / ".cache" / "opportunity_free_research", CACHE_TTL_SECONDS)
    cache_key = json.dumps([
        RESEARCH_POLICY_VERSION,
        evidence_fingerprint,
        query,
        [provider.cache_namespace for provider in selected],
    ], ensure_ascii=False)
    cached = cache.get(cache_key) if use_cache else None
    cache_hit = isinstance(cached, dict)
    api_requests = 0

    if cache_hit:
        citations = cached.get("citations") if isinstance(cached.get("citations"), list) else []
        failures = cached.get("provider_failures") if isinstance(cached.get("provider_failures"), list) else []
        attempted = cached.get("providers_attempted") if isinstance(cached.get("providers_attempted"), list) else []
        skipped_cooldown = cached.get("providers_skipped_cooldown") if isinstance(cached.get("providers_skipped_cooldown"), list) else []
        rejected_irrelevant = cached.get("rejected_irrelevant_sources") if isinstance(cached.get("rejected_irrelevant_sources"), list) else []
    else:
        papers: list[Paper] = []
        failures: list[dict[str, Any]] = []
        attempted: list[str] = []
        skipped_cooldown: list[dict[str, Any]] = []
        now_epoch = time.time()
        for provider in selected:
            cooldown = _provider_cooldown(root, provider.name, now_epoch)
            if cooldown is not None:
                skipped_cooldown.append(cooldown)
                continue
            attempted.append(provider.name)
            try:
                batch = provider.search(query, 3)
                api_requests += batch.api_requests
                papers.extend(batch.papers)
                if batch.papers:
                    _record_provider_strategy_feedback(
                        root,
                        provider_name=provider.name,
                        evidence_id=evidence_id,
                        succeeded=True,
                    )
                _clear_provider_cooldown(root, provider.name)
                for name, code in batch.provider_errors:
                    failures.append({"provider": name, "code": code, "retry_after_seconds": None})
                    _record_provider_strategy_feedback(
                        root,
                        provider_name=name,
                        evidence_id=evidence_id,
                        succeeded=False,
                        error_code=code,
                    )
                    if code in TRANSIENT_PROVIDER_CODES:
                        _record_provider_cooldown(root, name, ProviderError(code, True, request_count=1), now_epoch)
            except ProviderError as exc:
                api_requests += exc.request_count
                failures.append({
                    "provider": provider.name,
                    "code": exc.code,
                    "retry_after_seconds": exc.retry_after,
                })
                _record_provider_strategy_feedback(
                    root,
                    provider_name=provider.name,
                    evidence_id=evidence_id,
                    succeeded=False,
                    error_code=exc.code,
                )
                _record_provider_cooldown(root, provider.name, exc, now_epoch)
        unique = _dedupe(papers)
        raw_citations = [_citation(paper, f"R{index}") for index, paper in enumerate(unique, start=1)]
        citations, rejected_irrelevant = _filter_relevant_citations(evidence, raw_citations)

    quality = _quality(citations, failures, len(rejected_irrelevant))
    transient_failure = any(row.get("code") in TRANSIENT_PROVIDER_CODES for row in failures)
    if not cache_hit and use_cache and (quality["decision"] == "writer_ready" or not transient_failure):
        cache.put(cache_key, {
            "citations": citations,
            "provider_failures": failures,
            "providers_attempted": attempted,
            "providers_skipped_cooldown": skipped_cooldown,
            "rejected_irrelevant_sources": rejected_irrelevant,
        })
    citation_ids = [row["citation_id"] for row in citations]
    candidate_strategies = _strategies(evidence, citation_ids)
    local_rows = local_knowledge_context.get("results") if isinstance(local_knowledge_context.get("results"), list) else []
    for row in local_rows[:2]:
        if isinstance(row, Mapping) and isinstance(row.get("claim"), str):
            candidate_strategies.append({
                "strategy": ("Advisory verified local knowledge: " + row["claim"].strip())[:1200],
                "basis": "verified_local_knowledge_advisory",
                "citation_ids": [],
                "knowledge_keys": [row["knowledge_key"]] if isinstance(row.get("knowledge_key"), str) else [],
            })
    research_id = "or_" + uuid4().hex
    report = {
        "schema_version": 1,
        "kind": "opportunity_free_research_brief",
        "policy_version": RESEARCH_POLICY_VERSION,
        "research_id": research_id,
        "created_at": utc_now(),
        "evidence_id": evidence_id,
        "opportunity_id": evidence.get("opportunity", {}).get("opportunity_id"),
        "target": {
            "path": evidence.get("target", {}).get("path"),
            "symbol": evidence.get("target", {}).get("symbol"),
            "structural_goal": evidence.get("success_criteria", {}).get("structural_goal"),
            "benchmark_suite": evidence.get("success_criteria", {}).get("benchmark_suite"),
        },
        "query": query,
        "capability_broker": broker_plan,
        "providers_attempted": attempted,
        "provider_failures": failures,
        "providers_skipped_cooldown": skipped_cooldown,
        "citations": citations,
        "rejected_irrelevant_sources": rejected_irrelevant,
        "local_knowledge_context": local_knowledge_context,
        "candidate_strategies": candidate_strategies,
        "research_quality": quality,
        "writer_handoff_allowed": quality["decision"] == "writer_ready",
        "research_policy": (
            "free/public scholarly metadata only; local evidence remains authoritative for the code target; "
            "default provider selection is mediated by the non-authoritative Capability Broker; no metered provider is enabled here; "
            "no metered model call; transient provider failures enter a local cooldown; OpenAlex is an anonymous free-start fallback; "
            "target-specific relevance is scored locally before quality gating; unrelated sources cannot authorize writer handoff; "
            "provider rate limits/errors are recorded without retry flooding"
        ),
        "cache_hit": cache_hit,
        "api_requests": api_requests,
        "metered_model_requests": 0,
        "paid_spending": False,
    }
    artifact = root / "improvements" / "opportunities" / "research" / f"{research_id}.json"
    report["artifact"] = str(artifact)
    write_json(artifact, report)
    return report
