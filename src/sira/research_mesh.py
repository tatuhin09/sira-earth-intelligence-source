"""Free-first research mesh planning and bounded biomedical execution."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol

from .capability_broker import STATUS_READY, broker_decision
from .config import Settings, load_optional_key
from .language_intelligence import analyze_language_with_store
from .language_semantic_bridge import bridge_multilingual_query
from .models import ProviderError, utc_now
from .papers import Paper, PaperBatch
from .providers.europe_pmc import EuropePMCProvider
from .providers.pubmed import PubMedProvider
from .storage import Cache, RunStore, write_json


RESEARCH_MESH_POLICY_VERSION = 2
BIOMEDICAL_CACHE_TTL_SECONDS = 86400

DOMAIN_CAPABILITIES: Mapping[str, tuple[str, ...]] = {
    "biomedical": ("biomedical_search",),
    "knowledge": ("knowledge_search",),
    "open_access": ("open_access_search",),
    "scholarly": ("paper_search",),
    "software": ("repository_search",),
    "general_web": ("google_search_grounding", "web_search"),
    "url_reading": ("url_context", "web_extraction"),
}


class BiomedicalProvider(Protocol):
    name: str
    cache_namespace: str

    def search(self, query: str, max_results: int) -> PaperBatch: ...


def prepare_multilingual_research_query(
    root: Path,
    query: str,
    *,
    allow_model: bool = False,
    environ: Mapping[str, str] | None = None,
    use_cache: bool = True,
    model=None,
) -> dict[str, Any]:
    """Prepare one bounded research query without overstating translation quality."""
    root = Path(root).resolve()
    query = " ".join(str(query).split())
    if not query or len(query) > 500:
        raise ValueError("Question must contain 1..500 characters")

    profile = analyze_language_with_store(root, query)
    learned = (
        profile.learned_gloss
        if isinstance(profile.learned_gloss, str)
        and 1 <= len(profile.learned_gloss) <= 500
        else None
    )
    fallback = learned or profile.normalized_text

    if not profile.needs_semantic_teacher:
        return {
            "schema": "sira.multilingual_research_query.v1",
            "original_query": query,
            "effective_query": profile.normalized_text,
            "mode": "local_original",
            "language_label": profile.language_label,
            "teacher_required": False,
            "semantic_bridge_status": None,
            "translation_verified": False,
            "model_requests": 0,
            "authority_granted": False,
            "paid_spending": False,
        }

    if not allow_model:
        return {
            "schema": "sira.multilingual_research_query.v1",
            "original_query": query,
            "effective_query": fallback,
            "mode": "local_learned_gloss" if learned else "local_original",
            "language_label": profile.language_label,
            "teacher_required": True,
            "semantic_bridge_status": "model_not_enabled",
            "translation_verified": False,
            "model_requests": 0,
            "authority_granted": False,
            "paid_spending": False,
        }

    bridge = bridge_multilingual_query(
        root, query, allow_model=True, environ=environ,
        use_cache=use_cache, model=model,
    )
    metrics = bridge.get("metrics") if isinstance(bridge.get("metrics"), Mapping) else {}
    requests = (
        int(metrics.get("api_requests"))
        if isinstance(metrics.get("api_requests"), int)
        and not isinstance(metrics.get("api_requests"), bool)
        else 0
    )
    if bridge.get("status") == "completed":
        raw_queries = bridge.get("research_queries")
        candidates = [
            str(value).strip()
            for value in raw_queries
            if isinstance(value, str) and 1 <= len(value.strip()) <= 500
        ] if isinstance(raw_queries, list) else []
        canonical = bridge.get("canonical_english")
        if not candidates and isinstance(canonical, str) and 1 <= len(canonical.strip()) <= 500:
            candidates = [canonical.strip()]
        if candidates:
            return {
                "schema": "sira.multilingual_research_query.v1",
                "original_query": query,
                "effective_query": candidates[0],
                "mode": "verified_semantic_bridge",
                "language_label": profile.language_label,
                "teacher_required": True,
                "semantic_bridge_status": "completed",
                "translation_verified": True,
                "model_requests": requests,
                "authority_granted": False,
                "paid_spending": False,
            }

    return {
        "schema": "sira.multilingual_research_query.v1",
        "original_query": query,
        "effective_query": fallback,
        "mode": "local_learned_gloss" if learned else "local_original",
        "language_label": profile.language_label,
        "teacher_required": True,
        "semantic_bridge_status": str(bridge.get("status") or "failed"),
        "translation_verified": False,
        "model_requests": requests,
        "authority_granted": False,
        "paid_spending": False,
    }


def research_mesh_plan(
    root: Path,
    domain: str,
    *,
    allow_metered: bool = False,
    environ: Mapping[str, str] | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    root = Path(root).resolve()
    domain = str(domain).strip().casefold()
    capabilities = DOMAIN_CAPABILITIES.get(domain)
    if capabilities is None:
        raise ValueError("unknown research mesh domain")

    decisions = [
        broker_decision(
            root,
            capability,
            allow_metered=allow_metered,
            environ=environ,
            persist=persist,
        )
        for capability in capabilities
    ]
    ready = [row for row in decisions if row.get("status") == STATUS_READY]
    selected = [
        row.get("selected_provider_id")
        for row in ready
        if isinstance(row.get("selected_provider_id"), str)
    ]
    fallback_ids: list[str] = []
    for row in ready:
        raw = row.get("fallback_provider_ids")
        if isinstance(raw, list):
            for provider_id in raw:
                if (
                    isinstance(provider_id, str)
                    and provider_id not in selected
                    and provider_id not in fallback_ids
                ):
                    fallback_ids.append(provider_id)

    return {
        "schema": "sira.research_mesh_plan.v1",
        "policy_version": RESEARCH_MESH_POLICY_VERSION,
        "domain": domain,
        "capabilities": list(capabilities),
        "decisions": decisions,
        "ready_capability_count": len(ready),
        "selected_provider_ids": selected,
        "fallback_provider_ids": fallback_ids,
        "allow_metered": bool(allow_metered),
        "paid_spending": False,
        "provider_execution_performed": False,
        "access_request_created": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }


def _default_biomedical_providers(
    root: Path,
) -> tuple[tuple[BiomedicalProvider, ...], dict[str, Any]]:
    root = Path(root).resolve()
    settings = Settings(root, max_results=3)
    ncbi_key = load_optional_key(root, "SIRA_NCBI_API_KEY")
    ncbi_email = os.environ.get("SIRA_NCBI_EMAIL", "").strip() or None

    available: dict[str, BiomedicalProvider] = {
        "europe_pmc": EuropePMCProvider(timeout=settings.timeout_seconds),
        "pubmed": PubMedProvider(
            timeout=settings.timeout_seconds,
            api_key=ncbi_key,
            contact_email=ncbi_email,
        ),
    }

    plan = research_mesh_plan(
        root,
        "biomedical",
        allow_metered=False,
        persist=True,
    )
    chain = [*plan["selected_provider_ids"], *plan["fallback_provider_ids"]]
    providers = tuple(
        available[provider_id]
        for provider_id in chain
        if provider_id in available
    )
    if not providers:
        raise ValueError("Research Mesh has no usable free biomedical provider")
    return providers, plan


def _paper_key(paper: Paper) -> tuple[str, str]:
    if paper.doi:
        return ("doi", paper.doi.casefold())
    title = " ".join(paper.title.casefold().split())
    if title:
        return ("title", title)
    return ("url", paper.url.casefold())


def _paper_row(paper: Paper) -> dict[str, Any]:
    return {
        "paper_id": paper.paper_id,
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": list(paper.authors),
        "year": paper.year,
        "doi": paper.doi,
        "url": paper.url,
        "open_access_pdf_url": paper.open_access_pdf_url,
        "retrieved_at": paper.retrieved_at,
        "provider": paper.provider,
        "providers": list(paper.providers or (paper.provider,)),
    }


def _merge_rows(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key in ("abstract", "doi", "open_access_pdf_url", "year"):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    if not merged.get("authors") and incoming.get("authors"):
        merged["authors"] = incoming["authors"]
    providers = [
        *merged.get("providers", []),
        *incoming.get("providers", []),
        existing.get("provider"),
        incoming.get("provider"),
    ]
    merged["providers"] = list(dict.fromkeys(
        value for value in providers if isinstance(value, str) and value
    ))
    return merged


def search_biomedical_free(
    root: Path,
    query: str,
    *,
    max_results: int = 3,
    providers: tuple[BiomedicalProvider, ...] | None = None,
    use_cache: bool = True,
    allow_language_model: bool = False,
    language_environ: Mapping[str, str] | None = None,
    language_model=None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    original_query = " ".join(str(query).split())
    if not original_query or len(original_query) > 500:
        raise ValueError("Question must contain 1..500 characters")
    query_preparation = prepare_multilingual_research_query(
        root,
        original_query,
        allow_model=allow_language_model,
        environ=language_environ,
        use_cache=use_cache,
        model=language_model,
    )
    query = str(query_preparation["effective_query"])
    if type(max_results) is not int or not 1 <= max_results <= 3:
        raise ValueError("max_results must be 1..3")

    if providers is None:
        selected, plan = _default_biomedical_providers(root)
    else:
        selected = tuple(providers)
        if not selected:
            raise ValueError("At least one biomedical provider is required")
        plan = {
            "schema": "sira.research_mesh_plan.v1",
            "policy_version": RESEARCH_MESH_POLICY_VERSION,
            "domain": "biomedical",
            "capabilities": ["biomedical_search"],
            "decisions": [],
            "ready_capability_count": 1,
            "selected_provider_ids": [provider.name for provider in selected],
            "fallback_provider_ids": [],
            "allow_metered": False,
            "paid_spending": False,
            "provider_execution_performed": False,
            "access_request_created": False,
            "authority_granted": False,
            "promotion_authorized": False,
            "explicit_provider_override": True,
        }

    for provider in selected:
        if not getattr(provider, "name", "") or not getattr(provider, "cache_namespace", ""):
            raise ValueError("Biomedical providers require name and cache_namespace")

    cache = Cache(root / ".cache" / "research_mesh_biomedical", BIOMEDICAL_CACHE_TTL_SECONDS)
    cache_key = json.dumps(
        [
            RESEARCH_MESH_POLICY_VERSION,
            query,
            max_results,
            [provider.cache_namespace for provider in selected],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    cached = cache.get(cache_key) if use_cache else None

    run = RunStore(root)
    run.event("run_started", operation="research_mesh_biomedical")
    failures: list[dict[str, Any]] = []
    attempted: list[str] = []
    api_requests = 0
    cache_hit = isinstance(cached, dict)

    if cache_hit:
        results = cached.get("results") if isinstance(cached.get("results"), list) else []
        failures = cached.get("provider_failures") if isinstance(cached.get("provider_failures"), list) else []
        attempted = cached.get("providers_attempted") if isinstance(cached.get("providers_attempted"), list) else []
    else:
        rows: list[dict[str, Any]] = []
        positions: dict[tuple[str, str], int] = {}
        for provider in selected:
            attempted.append(provider.name)
            try:
                batch = provider.search(query, max_results)
                api_requests += batch.api_requests
                for paper in batch.papers:
                    key = _paper_key(paper)
                    row = _paper_row(paper)
                    if key in positions:
                        index = positions[key]
                        rows[index] = _merge_rows(rows[index], row)
                    else:
                        positions[key] = len(rows)
                        rows.append(row)
                for provider_name, code in batch.provider_errors:
                    failures.append({
                        "provider": provider_name,
                        "code": code,
                        "retry_after_seconds": None,
                    })
            except ProviderError as exc:
                api_requests += exc.request_count
                failures.append({
                    "provider": provider.name,
                    "code": exc.code,
                    "retry_after_seconds": exc.retry_after,
                })

        results = rows[: max_results * len(selected)]
        if use_cache and results:
            cache.put(cache_key, {
                "results": results,
                "provider_failures": failures,
                "providers_attempted": attempted,
            })

    status = (
        "completed"
        if results and not failures
        else "partial"
        if results
        else "failed"
    )
    report = {
        "schema_version": 1,
        "kind": "research_mesh_biomedical_search",
        "policy_version": RESEARCH_MESH_POLICY_VERSION,
        "run_id": run.run_id,
        "created_at": utc_now(),
        "original_query": original_query,
        "query": query,
        "query_preparation": query_preparation,
        "status": status,
        "mesh_plan": plan,
        "providers_attempted": attempted,
        "provider_failures": failures,
        "results": results,
        "metrics": {
            "result_count": len(results),
            "provider_count": len(selected),
            "api_requests": api_requests,
            "cache_hit": cache_hit,
        },
        "paid_spending": False,
        "metered_provider_requests": 0,
        "authority_granted": False,
        "promotion_authorized": False,
    }
    write_json(run.path / "research-mesh.json", report)
    run.event("run_finished", status=status, result_count=len(results))
    return report
