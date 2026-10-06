"""Bounded scholarly-paper retrieval with rich metadata and local caching."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from time import perf_counter
from typing import Protocol

from . import __version__
from .config import Settings
from .models import ProviderError, canonical_url, utc_now
from .storage import Cache, RunStore, code_digest, write_json


def _normalize_paper_authors(authors: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(authors, tuple) or len(authors) > 500:
        raise ValueError("authors must be a bounded tuple")
    clean_authors = []
    for author in authors:
        if not isinstance(author, str) or not author.strip() or len(author) > 1000:
            raise ValueError("author names must be nonempty bounded strings")
        clean_authors.append(author.strip())
    return tuple(clean_authors)


def _normalize_paper_providers(providers: tuple[str, ...], provider: str) -> tuple[str, ...]:
    if not isinstance(providers, tuple) or len(providers) > 20:
        raise ValueError("providers must be a bounded tuple")
    values = providers or (provider,)
    clean_providers = []
    seen_providers = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 100:
            raise ValueError("providers must contain nonempty bounded text")
        value = value.strip()
        folded = value.casefold()
        if folded not in seen_providers:
            clean_providers.append(value)
            seen_providers.add(folded)
    return tuple(clean_providers)


@dataclass(frozen=True)
class Paper:
    paper_id: str
    title: str
    abstract: str | None
    authors: tuple[str, ...]
    year: int | None
    doi: str | None
    url: str
    open_access_pdf_url: str | None
    retrieved_at: str
    provider: str = "unknown"
    published_date: str | None = None
    providers: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.paper_id, str) or not self.paper_id.strip() or len(self.paper_id) > 256:
            raise ValueError("paper_id must be a nonempty bounded string")
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 4000:
            raise ValueError("title must be a nonempty bounded string")
        if self.abstract is not None and (not isinstance(self.abstract, str) or len(self.abstract) > 100_000):
            raise ValueError("abstract must be text or null")
        clean_authors = _normalize_paper_authors(self.authors)
        if self.year is not None and (type(self.year) is not int or not 1 <= self.year <= 9999):
            raise ValueError("year must be an integer or null")
        if self.doi is not None and (not isinstance(self.doi, str) or not self.doi.strip()
                                     or len(self.doi) > 1000 or any(ord(c) < 32 for c in self.doi)):
            raise ValueError("doi must be bounded text or null")
        if not isinstance(self.retrieved_at, str) or not self.retrieved_at.strip():
            raise ValueError("retrieved_at must be nonempty text")
        if not isinstance(self.provider, str) or not self.provider.strip() or len(self.provider) > 100:
            raise ValueError("provider must be nonempty bounded text")
        if self.published_date is not None:
            if (not isinstance(self.published_date, str) or len(self.published_date) != 10
                    or self.published_date[4:5] != "-" or self.published_date[7:8] != "-"):
                raise ValueError("published_date must be YYYY-MM-DD or null")
            try:
                from datetime import date
                date.fromisoformat(self.published_date)
            except ValueError:
                raise ValueError("published_date must be YYYY-MM-DD or null") from None
        clean_providers = _normalize_paper_providers(self.providers, self.provider)
        object.__setattr__(self, "paper_id", self.paper_id.strip())
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "abstract", self.abstract.strip() if isinstance(self.abstract, str) else None)
        object.__setattr__(self, "authors", clean_authors)
        object.__setattr__(self, "doi", self.doi.strip() if isinstance(self.doi, str) else None)
        object.__setattr__(self, "provider", self.provider.strip())
        object.__setattr__(self, "providers", clean_providers)
        object.__setattr__(self, "url", canonical_url(self.url))
        if self.open_access_pdf_url is not None:
            object.__setattr__(self, "open_access_pdf_url", canonical_url(self.open_access_pdf_url))


@dataclass(frozen=True)
class PaperBatch:
    papers: tuple[Paper, ...]
    api_requests: int = 0
    rejected_papers: int = 0
    providers_attempted: tuple[str, ...] = ()
    fallback_used: bool = False
    provider_errors: tuple[tuple[str, str], ...] = ()
    terminal_error: str | None = None
    enrichment_used: bool = False
    papers_merged: int = 0
    papers_enriched: int = 0


class PaperProvider(Protocol):
    name: str
    cache_namespace: str

    def search(self, query: str, max_results: int) -> PaperBatch: ...


def _paper_from_cache(row: dict) -> Paper:
    values = dict(row)
    values["authors"] = tuple(values["authors"])
    if "providers" in values:
        values["providers"] = tuple(values["providers"])
    return Paper(**values)


def _identity_keys(paper: Paper) -> tuple[str, ...]:
    keys = ["id:" + paper.provider.casefold() + ":" + paper.paper_id.casefold(),
            "url:" + paper.url.casefold()]
    if paper.doi:
        keys.append("doi:" + paper.doi.casefold())
    return tuple(keys)


def papers_run(root: Path, query: str, provider: PaperProvider, settings: Settings, *, use_cache=True) -> Path:
    """Run one bounded paper search and persist a provider-neutral artifact."""
    query = " ".join(query.split())
    if not query or len(query) > 500:
        raise ValueError("Question must contain 1..500 characters")
    if not 1 <= settings.max_results <= 3:
        raise ValueError("Paper max_results must be 1..3")

    started = perf_counter()
    run = RunStore(root)
    run.event("run_started", provider=provider.name, kind="paper_search")
    cache = Cache(root / ".cache" / "papers", settings.cache_ttl_seconds)
    cache_key = json.dumps([1, provider.cache_namespace, query, settings.max_results], ensure_ascii=False)
    batch, error, status, cache_hit = PaperBatch(()), None, "completed", False

    try:
        cached = cache.get(cache_key) if use_cache else None
        if cached is not None:
            try:
                batch = PaperBatch(
                    tuple(_paper_from_cache(row) for row in cached["papers"]),
                    api_requests=0,
                    rejected_papers=int(cached["rejected_papers"]),
                    providers_attempted=tuple(cached.get("providers_attempted", ())),
                    fallback_used=bool(cached.get("fallback_used", False)),
                    provider_errors=tuple(tuple(item) for item in cached.get("provider_errors", ())),
                    terminal_error=None,
                    enrichment_used=bool(cached.get("enrichment_used", False)),
                    papers_merged=int(cached.get("papers_merged", 0)),
                    papers_enriched=int(cached.get("papers_enriched", 0)),
                )
                cache_hit = True
            except (KeyError, TypeError, ValueError, OverflowError):
                run.event("cache_invalid")
        if cache_hit:
            run.event("cache_hit")
        else:
            run.event("provider_started")
            batch = provider.search(query, settings.max_results)
            if batch.terminal_error is not None:
                status = "failed"
                error = {"code": batch.terminal_error, "retry_after_seconds": None}
                run.event("provider_failed", **error)
            else:
                if use_cache:
                    cache.put(cache_key, {
                        "papers": [asdict(paper) for paper in batch.papers],
                        "rejected_papers": batch.rejected_papers,
                        "providers_attempted": list(batch.providers_attempted),
                        "fallback_used": batch.fallback_used,
                        "provider_errors": [list(item) for item in batch.provider_errors],
                        "enrichment_used": batch.enrichment_used,
                        "papers_merged": batch.papers_merged,
                        "papers_enriched": batch.papers_enriched,
                    })
                run.event("provider_finished", api_requests=batch.api_requests)
    except ProviderError as exc:
        status = "failed"
        error = {"code": exc.code, "retry_after_seconds": exc.retry_after}
        batch = PaperBatch((), exc.request_count, 0,
                           providers_attempted=(provider.name,),
                           provider_errors=((provider.name, exc.code),))
        run.event("provider_failed", **error)
    except KeyboardInterrupt:
        status, error = "cancelled", {"code": "user_cancelled"}
        run.event("run_cancelled")

    seen: set[str] = set()
    papers: list[Paper] = []
    duplicates_removed = 0
    for paper in batch.papers:
        keys = _identity_keys(paper)
        if any(key in seen for key in keys):
            duplicates_removed += 1
            continue
        seen.update(keys)
        papers.append(paper)
        if len(papers) >= settings.max_results:
            break

    if status == "completed" and not papers:
        status = "no_results"
    cancelled = status == "cancelled"
    metrics = {
        "paper_count": len(papers),
        "duplicates_removed": duplicates_removed,
        "rejected_papers": batch.rejected_papers,
        "abstracts_available": sum(p.abstract is not None for p in papers),
        "doi_available": sum(p.doi is not None for p in papers),
        "open_access_pdf_available": sum(p.open_access_pdf_url is not None for p in papers),
        "latency_seconds": round(perf_counter() - started, 6),
        "api_requests": None if cancelled else batch.api_requests,
        "cache_hit": cache_hit,
        "failures": int(status == "failed"),
        "providers_attempted": list(batch.providers_attempted or ((provider.name,) if batch.api_requests else ())),
        "fallback_used": batch.fallback_used,
        "provider_failures": len(batch.provider_errors),
        "enrichment_used": batch.enrichment_used,
        "papers_merged": batch.papers_merged,
        "papers_enriched": batch.papers_enriched,
    }
    result = {
        "schema_version": 1,
        "sira_version": __version__,
        "run_id": run.run_id,
        "code_sha256": code_digest(),
        "recorded_at": utc_now(),
        "question": query,
        "provider": provider.name,
        "provider_config": provider.cache_namespace,
        "settings": {
            "max_results": settings.max_results,
            "timeout_seconds": settings.timeout_seconds,
            "cache_ttl_seconds": settings.cache_ttl_seconds,
            "use_cache": use_cache,
        },
        "status": status,
        "error": error,
        "output_kind": "scholarly_metadata_not_fact_checked",
        "papers": [asdict(paper) for paper in papers],
        "metrics": metrics,
    }
    write_json(run.path / "papers.json", result)
    run.event("run_finished", status=status)
    return run.path
