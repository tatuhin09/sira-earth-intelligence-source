"""Single research retrieval run; provider text has no execution path."""
from dataclasses import asdict
import json
from time import perf_counter
from urllib.parse import urlsplit

from . import __version__
from .config import Settings
from .models import ProviderError, SearchBatch, SearchProvider, Source, utc_now
from .storage import Cache, RunStore, code_digest, write_json


def research(query: str, provider: SearchProvider, settings: Settings, *, use_cache=True):
    query = " ".join(query.split())
    if not query or len(query) > 500:
        raise ValueError("Question must contain 1..500 characters")
    started = perf_counter()
    run = RunStore(settings.root)
    run.event("run_started", provider=provider.name)
    cache = Cache(settings.root / ".cache", settings.cache_ttl_seconds)
    cache_key = json.dumps([1, provider.cache_namespace, query, settings.max_results])
    batch, error, status, cache_hit = SearchBatch(()), None, "completed", False
    try:
        cached = cache.get(cache_key) if use_cache else None
        if cached is not None:
            try:
                batch = SearchBatch(tuple(Source(**s) for s in cached["sources"]),
                                    rejected_sources=cached["rejected_sources"])
                cache_hit = True
            except (KeyError, TypeError, ValueError):
                run.event("cache_invalid")
        if cache_hit:
            run.event("cache_hit")
        else:
            run.event("provider_started")
            batch = provider.search(query, settings.max_results)
            if use_cache:
                cache.put(cache_key, {"sources": [asdict(s) for s in batch.sources],
                                      "rejected_sources": batch.rejected_sources})
            run.event("provider_finished", api_requests=batch.api_requests)
    except ProviderError as exc:
        status = "failed"
        error = {"code": exc.code, "retry_after_seconds": exc.retry_after}
        batch = SearchBatch((), int(exc.request_sent), None if exc.request_sent else 0)
        run.event("provider_failed", **error)
    except KeyboardInterrupt:
        status, error = "cancelled", {"code": "user_cancelled"}
        run.event("run_cancelled")
    # Filesystem/programming errors propagate; the CLI emits a safe diagnostic.

    unique = {}
    for source in batch.sources:
        unique.setdefault(source.url, source)
    sources = [{"id": f"S{i}", **asdict(source), "trust": "untrusted",
                "verification": "url_format_only", "provider": provider.name}
               for i, source in enumerate(unique.values(), 1)]
    citations = [{"id": f"C{i}", "source_id": s["id"], "url": s["url"]}
                 for i, s in enumerate(sources, 1)]
    if status == "completed" and not sources:
        status = "no_results"
    cancelled = status == "cancelled"
    metrics = {
        "factual_accuracy": None, "citation_correctness": None,
        "citation_coverage": None, "source_quality": None, "source_diversity": None,
        "citation_reference_resolution": 1.0 if citations else None,
        "unique_hostnames": len({urlsplit(s["url"]).hostname for s in sources}),
        "retrieval_success": bool(sources), "source_count": len(sources),
        "duplicates_removed": len(batch.sources) - len(sources),
        "rejected_sources": batch.rejected_sources,
        "latency_seconds": round(perf_counter() - started, 6),
        "api_requests": None if cancelled else batch.api_requests,
        "reported_credits": None if cancelled else batch.credits,
        "failures": int(status == "failed"), "cache_hit": cache_hit,
    }
    result = {
        "schema_version": 1, "sira_version": __version__, "run_id": run.run_id,
        "code_sha256": code_digest(), "recorded_at": utc_now(), "question": query,
        "provider": provider.name, "provider_config": provider.cache_namespace,
        "settings": {"max_results": settings.max_results, "timeout_seconds": settings.timeout_seconds,
                     "cache_ttl_seconds": settings.cache_ttl_seconds, "use_cache": use_cache},
        "status": status, "error": error,
        "output_kind": "unverified_retrieval_evidence", "sources": sources,
        "citations": citations, "metrics": metrics,
    }
    write_json(run.path / "result.json", result)
    run.event("run_finished", status=status)
    return run.path
