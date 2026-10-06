"""Sequential, free-first scholarly metadata failover across paper providers."""
from collections.abc import Iterable

from ..models import ProviderError
from ..papers import Paper, PaperBatch, PaperProvider


class FallbackPaperProvider:
    name = "paper_auto"

    def __init__(self, providers: Iterable[PaperProvider]):
        self.providers = tuple(providers)
        if not self.providers:
            raise ValueError("at least one paper provider is required")
        if any(not getattr(provider, "name", "") or not getattr(provider, "cache_namespace", "")
               for provider in self.providers):
            raise ValueError("paper providers require name and cache_namespace")
        self.cache_namespace = "paper-auto-v1:" + "|".join(
            provider.cache_namespace for provider in self.providers)

    def search(self, query: str, max_results: int) -> PaperBatch:
        if not 1 <= max_results <= 3:
            raise ValueError("Paper max_results must be 1..3")
        attempts: list[str] = []
        errors: list[tuple[str, str]] = []
        total_requests = 0
        total_rejected = 0
        any_success = False

        for index, provider in enumerate(self.providers):
            attempts.append(provider.name)
            try:
                batch = provider.search(query, max_results)
                any_success = True
                total_requests += batch.api_requests
                total_rejected += batch.rejected_papers
                if batch.papers:
                    return PaperBatch(
                        batch.papers,
                        api_requests=total_requests,
                        rejected_papers=total_rejected,
                        providers_attempted=tuple(attempts),
                        fallback_used=index > 0,
                        provider_errors=tuple(errors),
                    )
            except ProviderError as exc:
                total_requests += exc.request_count
                errors.append((provider.name, exc.code))

        if errors and not any_success:
            return PaperBatch(
                (),
                api_requests=total_requests,
                rejected_papers=total_rejected,
                providers_attempted=tuple(attempts),
                fallback_used=len(attempts) > 1,
                provider_errors=tuple(errors),
                terminal_error="all_paper_providers_failed",
            )
        return PaperBatch(
            (),
            api_requests=total_requests,
            rejected_papers=total_rejected,
            providers_attempted=tuple(attempts),
            fallback_used=len(attempts) > 1,
            provider_errors=tuple(errors),
        )


class EnrichingPaperProvider:
    """Use arXiv-like metadata to enrich or recover a bounded base result set."""
    name = "paper_enriched_auto"

    def __init__(self, base: PaperProvider, enricher: PaperProvider):
        for provider in (base, enricher):
            if not getattr(provider, "name", "") or not getattr(provider, "cache_namespace", ""):
                raise ValueError("paper providers require name and cache_namespace")
        self.base = base
        self.enricher = enricher
        self.cache_namespace = (
            "paper-enriched-v1:" + base.cache_namespace + "|" + enricher.cache_namespace
        )

    @staticmethod
    def _normalized_title(value: str) -> str:
        import re
        return " ".join(re.findall(r"[^\W_]+", value.casefold(), re.UNICODE))

    @classmethod
    def _same_work(cls, left: Paper, right: Paper) -> bool:
        if left.doi and right.doi and left.doi.casefold() == right.doi.casefold():
            return True
        return cls._normalized_title(left.title) == cls._normalized_title(right.title)

    @staticmethod
    def _merge(primary: Paper, extra: Paper) -> tuple[Paper, bool]:
        from dataclasses import replace

        providers = tuple(dict.fromkeys((*primary.providers, *extra.providers)))
        updates = {
            "abstract": primary.abstract or extra.abstract,
            "authors": primary.authors or extra.authors,
            "year": primary.year or extra.year,
            "doi": primary.doi or extra.doi,
            "open_access_pdf_url": primary.open_access_pdf_url or extra.open_access_pdf_url,
            "published_date": primary.published_date or extra.published_date,
            "providers": providers,
        }
        changed = any(getattr(primary, field) != value for field, value in updates.items())
        return replace(primary, **updates), changed

    @staticmethod
    def _score(query: str, paper: Paper) -> tuple[float, int]:
        import re
        query_terms = set(re.findall(r"[^\W_]+", query.casefold(), re.UNICODE))
        title_terms = set(re.findall(r"[^\W_]+", paper.title.casefold(), re.UNICODE))
        abstract_terms = set(re.findall(r"[^\W_]+", (paper.abstract or "").casefold(), re.UNICODE))
        denom = max(1, len(query_terms))
        title_overlap = len(query_terms & title_terms) / denom
        abstract_overlap = len(query_terms & abstract_terms) / denom
        richness = sum((paper.abstract is not None, paper.open_access_pdf_url is not None,
                        paper.doi is not None, paper.year is not None, paper.published_date is not None))
        return (title_overlap * 100.0 + abstract_overlap * 10.0 + richness, richness)

    @staticmethod
    def _attempts(batch: PaperBatch, provider: PaperProvider) -> tuple[str, ...]:
        return batch.providers_attempted or ((provider.name,) if batch.api_requests else ())

    def search(self, query: str, max_results: int) -> PaperBatch:
        if not 1 <= max_results <= 3:
            raise ValueError("Paper max_results must be 1..3")

        try:
            base = self.base.search(query, max_results)
        except ProviderError as exc:
            base = PaperBatch((), api_requests=exc.request_count,
                              providers_attempted=(self.base.name,),
                              provider_errors=((self.base.name, exc.code),),
                              terminal_error="all_paper_providers_failed")

        base_papers = list(base.papers)
        need_enrichment = (
            base.terminal_error is not None
            or len(base_papers) < max_results
            or any(p.abstract is None or p.open_access_pdf_url is None for p in base_papers)
        )
        if not need_enrichment:
            return PaperBatch(
                tuple(base_papers[:max_results]),
                api_requests=base.api_requests,
                rejected_papers=base.rejected_papers,
                providers_attempted=self._attempts(base, self.base),
                fallback_used=base.fallback_used,
                provider_errors=base.provider_errors,
                terminal_error=base.terminal_error,
                enrichment_used=False,
                papers_merged=base.papers_merged,
                papers_enriched=base.papers_enriched,
            )

        try:
            enrichment = self.enricher.search(query, max_results)
        except ProviderError as exc:
            enrichment = PaperBatch((), api_requests=exc.request_count,
                                    providers_attempted=(self.enricher.name,),
                                    provider_errors=((self.enricher.name, exc.code),),
                                    terminal_error="all_paper_providers_failed")

        merged = list(base_papers)
        merged_count = 0
        enriched_count = 0
        for extra in enrichment.papers:
            match_index = next((i for i, existing in enumerate(merged)
                                if self._same_work(existing, extra)), None)
            if match_index is None:
                merged.append(extra)
                continue
            merged_count += 1
            combined, changed = self._merge(merged[match_index], extra)
            merged[match_index] = combined
            enriched_count += int(changed)

        ranked = sorted(enumerate(merged), key=lambda pair: (self._score(query, pair[1]), -pair[0]),
                        reverse=True)
        papers = tuple(paper for _, paper in ranked[:max_results])

        attempts = (*self._attempts(base, self.base), *self._attempts(enrichment, self.enricher))
        errors = (*base.provider_errors, *enrichment.provider_errors)
        recovered = bool(papers)
        terminal_error = None if recovered else (
            "all_paper_providers_failed"
            if base.terminal_error is not None and enrichment.terminal_error is not None
            else None
        )
        return PaperBatch(
            papers,
            api_requests=base.api_requests + enrichment.api_requests,
            rejected_papers=base.rejected_papers + enrichment.rejected_papers,
            providers_attempted=tuple(dict.fromkeys(attempts)),
            fallback_used=base.fallback_used or (not base_papers and bool(enrichment.papers)),
            provider_errors=tuple(errors),
            terminal_error=terminal_error,
            enrichment_used=True,
            papers_merged=base.papers_merged + enrichment.papers_merged + merged_count,
            papers_enriched=base.papers_enriched + enrichment.papers_enriched + enriched_count,
        )
