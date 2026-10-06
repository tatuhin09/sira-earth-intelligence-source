from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Mapping, Protocol
from .config import Settings
from .language_semantic_bridge import bridge_multilingual_query
from .language_intelligence import analyze_language_with_store
from .models import ProviderError, utc_now
from .papers import PaperBatch
from .providers.doaj import DOAJProvider
from .providers.wikimedia import KnowledgeBatch, KnowledgeRecord, WikimediaProvider
from .research_mesh import research_mesh_plan
from .storage import Cache, RunStore, write_json

CACHE_TTL_SECONDS = 86400

class KnowledgeProvider(Protocol):
    name: str
    cache_namespace: str
    def search(self, query: str, max_results: int) -> KnowledgeBatch: ...

class OpenAccessProvider(Protocol):
    name: str
    cache_namespace: str
    def search(self, query: str, max_results: int) -> PaperBatch: ...

def _prepare_query(root, query, *, allow_model=False, environ=None, use_cache=True, model=None):
    query = " ".join(str(query).split())
    if not query or len(query) > 500:
        raise ValueError("Question must contain 1..500 characters")
    profile = analyze_language_with_store(root, query)
    fallback = profile.learned_gloss or profile.normalized_text
    if not profile.needs_semantic_teacher:
        return {
            "original_query": query,
            "effective_query": profile.normalized_text,
            "mode": "local_original",
            "model_requests": 0,
            "translation_verified": False,
        }
    if not allow_model:
        return {
            "original_query": query,
            "effective_query": fallback,
            "mode": "local_learned_gloss" if profile.learned_gloss else "local_original",
            "model_requests": 0,
            "translation_verified": False,
        }
    bridge = bridge_multilingual_query(
        root, query, allow_model=True, environ=environ,
        use_cache=use_cache, model=model
    )
    metrics = bridge.get("metrics") if isinstance(bridge.get("metrics"), Mapping) else {}
    requests = metrics.get("api_requests")
    requests = requests if type(requests) is int else 0
    if bridge.get("status") == "completed":
        queries = bridge.get("research_queries")
        if isinstance(queries, list):
            clean = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
            if clean:
                return {
                    "original_query": query,
                    "effective_query": clean[0][:500],
                    "mode": "verified_semantic_bridge",
                    "model_requests": requests,
                    "translation_verified": True,
                }
    return {
        "original_query": query,
        "effective_query": fallback,
        "mode": "safe_local_fallback",
        "model_requests": requests,
        "translation_verified": False,
    }

def _plan_override(domain, capability, providers):
    return {
        "schema": "sira.research_mesh_plan.v1",
        "domain": domain,
        "capabilities": [capability],
        "selected_provider_ids": [p.name for p in providers],
        "fallback_provider_ids": [],
        "allow_metered": False,
        "paid_spending": False,
        "authority_granted": False,
        "promotion_authorized": False,
        "explicit_provider_override": True,
    }

def search_knowledge_free(
    root: Path, query: str, *, max_results: int = 3,
    providers: tuple[KnowledgeProvider, ...] | None = None,
    use_cache: bool = True, allow_language_model: bool = False,
    language_environ=None, language_model=None,
):
    root = Path(root).resolve()
    if type(max_results) is not int or not 1 <= max_results <= 3:
        raise ValueError("max_results must be 1..3")
    prep = _prepare_query(
        root, query, allow_model=allow_language_model,
        environ=language_environ, use_cache=use_cache, model=language_model
    )
    if providers is None:
        settings = Settings(root, max_results=3)
        providers = (WikimediaProvider(timeout=settings.timeout_seconds),)
        plan = research_mesh_plan(root, "knowledge", allow_metered=False, persist=True)
    else:
        providers = tuple(providers)
        if not providers:
            raise ValueError("knowledge providers required")
        plan = _plan_override("knowledge", "knowledge_search", providers)

    cache = Cache(root / ".cache" / "knowledge_mesh", CACHE_TTL_SECONDS)
    key = json.dumps([
        "v1", prep["effective_query"], max_results,
        [p.cache_namespace for p in providers]
    ], ensure_ascii=False, separators=(",", ":"))
    cached = cache.get(key) if use_cache else None
    run = RunStore(root)
    failures = []
    attempts = []
    api_requests = 0

    if isinstance(cached, dict):
        results = cached.get("results", [])
        failures = cached.get("provider_failures", [])
        attempts = cached.get("providers_attempted", [])
        cache_hit = True
    else:
        cache_hit = False
        results = []
        seen = set()
        for provider in providers:
            attempts.append(provider.name)
            try:
                batch = provider.search(prep["effective_query"], max_results)
                api_requests += batch.api_requests
                for record in batch.records:
                    if record.url.casefold() in seen:
                        continue
                    seen.add(record.url.casefold())
                    results.append(record.to_dict())
            except ProviderError as exc:
                api_requests += exc.request_count
                failures.append({
                    "provider": provider.name,
                    "code": exc.code,
                    "retry_after_seconds": exc.retry_after,
                })
        results = results[: max_results * len(providers)]
        if use_cache and results:
            cache.put(key, {
                "results": results,
                "provider_failures": failures,
                "providers_attempted": attempts,
            })

    status = "completed" if results and not failures else "partial" if results else "failed"
    report = {
        "schema_version": 1,
        "kind": "knowledge_mesh_search",
        "run_id": run.run_id,
        "created_at": utc_now(),
        "original_query": prep["original_query"],
        "query": prep["effective_query"],
        "query_preparation": prep,
        "status": status,
        "mesh_plan": plan,
        "providers_attempted": attempts,
        "provider_failures": failures,
        "results": results,
        "metrics": {
            "result_count": len(results),
            "provider_count": len(providers),
            "api_requests": api_requests,
            "language_model_requests": prep["model_requests"],
            "cache_hit": cache_hit,
        },
        "paid_spending": False,
        "metered_provider_requests": 0,
        "authority_granted": False,
        "promotion_authorized": False,
    }
    write_json(run.path / "knowledge-mesh.json", report)
    return report

def _paper_row(p):
    return {
        "paper_id": p.paper_id,
        "title": p.title,
        "abstract": p.abstract,
        "authors": list(p.authors),
        "year": p.year,
        "doi": p.doi,
        "url": p.url,
        "open_access_pdf_url": p.open_access_pdf_url,
        "retrieved_at": p.retrieved_at,
        "provider": p.provider,
        "providers": list(p.providers or (p.provider,)),
    }

def search_open_access_free(
    root: Path, query: str, *, max_results: int = 3,
    providers: tuple[OpenAccessProvider, ...] | None = None,
    use_cache: bool = True, allow_language_model: bool = False,
    language_environ=None, language_model=None,
):
    root = Path(root).resolve()
    if type(max_results) is not int or not 1 <= max_results <= 3:
        raise ValueError("max_results must be 1..3")
    prep = _prepare_query(
        root, query, allow_model=allow_language_model,
        environ=language_environ, use_cache=use_cache, model=language_model
    )
    if providers is None:
        settings = Settings(root, max_results=3)
        providers = (DOAJProvider(timeout=settings.timeout_seconds),)
        plan = research_mesh_plan(root, "open_access", allow_metered=False, persist=True)
    else:
        providers = tuple(providers)
        if not providers:
            raise ValueError("open access providers required")
        plan = _plan_override("open_access", "open_access_search", providers)

    cache = Cache(root / ".cache" / "open_access_mesh", CACHE_TTL_SECONDS)
    key = json.dumps([
        "v1", prep["effective_query"], max_results,
        [p.cache_namespace for p in providers]
    ], ensure_ascii=False, separators=(",", ":"))
    cached = cache.get(key) if use_cache else None
    run = RunStore(root)
    failures = []
    attempts = []
    api_requests = 0

    if isinstance(cached, dict):
        results = cached.get("results", [])
        failures = cached.get("provider_failures", [])
        attempts = cached.get("providers_attempted", [])
        cache_hit = True
    else:
        cache_hit = False
        results = []
        seen = set()
        for provider in providers:
            attempts.append(provider.name)
            try:
                batch = provider.search(prep["effective_query"], max_results)
                api_requests += batch.api_requests
                for paper in batch.papers:
                    k = ("doi", paper.doi.casefold()) if paper.doi else ("title", paper.title.casefold())
                    if k in seen:
                        continue
                    seen.add(k)
                    results.append(_paper_row(paper))
            except ProviderError as exc:
                api_requests += exc.request_count
                failures.append({
                    "provider": provider.name,
                    "code": exc.code,
                    "retry_after_seconds": exc.retry_after,
                })
        results = results[: max_results * len(providers)]
        if use_cache and results:
            cache.put(key, {
                "results": results,
                "provider_failures": failures,
                "providers_attempted": attempts,
            })

    status = "completed" if results and not failures else "partial" if results else "failed"
    report = {
        "schema_version": 1,
        "kind": "open_access_mesh_search",
        "run_id": run.run_id,
        "created_at": utc_now(),
        "original_query": prep["original_query"],
        "query": prep["effective_query"],
        "query_preparation": prep,
        "status": status,
        "mesh_plan": plan,
        "providers_attempted": attempts,
        "provider_failures": failures,
        "results": results,
        "metrics": {
            "result_count": len(results),
            "provider_count": len(providers),
            "api_requests": api_requests,
            "language_model_requests": prep["model_requests"],
            "cache_hit": cache_hit,
        },
        "paid_spending": False,
        "metered_provider_requests": 0,
        "authority_granted": False,
        "promotion_authorized": False,
    }
    write_json(run.path / "open-access-mesh.json", report)
    return report
