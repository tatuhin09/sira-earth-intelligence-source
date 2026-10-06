"""Bounded Semantic Scholar paper search with optional key and short 429 retry."""
from urllib.parse import urlencode
from time import sleep
from urllib.request import Request

from ..models import ProviderError, canonical_url, utc_now
from ..papers import Paper, PaperBatch
from .http_json import open_request, request_json


class SemanticScholarProvider:
    name = "semantic_scholar"
    cache_namespace = "semantic-scholar-relevance-v1"
    _endpoint = "https://api.semanticscholar.org/graph/v1/paper/search"
    _fields = "title,abstract,authors,year,url,externalIds,openAccessPdf"

    def __init__(self, api_key: str | None = None, timeout: int = 20, *, max_attempts: int = 3):
        if not 1 <= timeout <= 60:
            raise ValueError("timeout must be 1..60 seconds")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be 1..3")
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
            raise ValueError("api_key must be nonempty text or null")
        self.api_key = api_key.strip() if isinstance(api_key, str) else None
        self.timeout = timeout
        self.max_attempts = max_attempts

    @staticmethod
    def _query(value: str) -> str:
        value = " ".join(value.split())
        if not value or len(value) > 500:
            raise ValueError("Question must contain 1..500 characters")
        # Semantic Scholar documents that hyphenated search terms do not match;
        # preserve the stored user question but normalize the provider query.
        return " ".join(value.replace("-", " ").split())

    @staticmethod
    def _optional_pdf(row) -> str | None:
        if not isinstance(row, dict):
            return None
        value = row.get("url")
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return canonical_url(value)
        except ValueError:
            return None

    @staticmethod
    def _paper(row: dict, retrieved_at: str) -> Paper:
        if not isinstance(row, dict):
            raise ValueError("paper row must be an object")
        abstract = row.get("abstract")
        if abstract is not None and not isinstance(abstract, str):
            raise ValueError("abstract must be text or null")
        authors_raw = row.get("authors")
        if authors_raw is None:
            authors_raw = []
        if not isinstance(authors_raw, list):
            raise ValueError("authors must be a list")
        authors = []
        for author in authors_raw[:500]:
            if isinstance(author, dict) and isinstance(author.get("name"), str):
                name = author["name"].strip()
                if name and len(name) <= 1000:
                    authors.append(name)

        year = row.get("year")
        if type(year) is not int or not 1 <= year <= 9999:
            year = None
        external = row.get("externalIds")
        doi = external.get("DOI") if isinstance(external, dict) else None
        if isinstance(doi, str):
            doi = doi.strip()
            if not doi or len(doi) > 1000 or any(ord(c) < 32 for c in doi):
                doi = None
        else:
            doi = None

        return Paper(
            paper_id=row["paperId"],
            title=row["title"],
            abstract=abstract,
            authors=tuple(authors),
            year=year,
            doi=doi,
            url=row["url"],
            open_access_pdf_url=SemanticScholarProvider._optional_pdf(row.get("openAccessPdf")),
            retrieved_at=retrieved_at,
            provider="semantic_scholar",
        )

    def search(self, query: str, max_results: int) -> PaperBatch:
        if not 1 <= max_results <= 3:
            raise ValueError("Paper max_results must be 1..3")
        params = urlencode({
            "query": self._query(query),
            "limit": max_results,
            "fields": self._fields,
        })
        headers = {"Accept": "application/json", "User-Agent": "SIRA/0.6"}
        if self.api_key is not None:
            headers["x-api-key"] = self.api_key
        request = Request(
            f"{self._endpoint}?{params}",
            method="GET",
            headers=headers,
        )

        attempts = 0
        while True:
            attempts += 1
            try:
                payload = request_json(request, self.timeout, open_request)
                break
            except ProviderError as exc:
                if exc.code != "http_429":
                    exc.request_count = attempts
                    raise
                retry_after = exc.retry_after
                if attempts >= self.max_attempts or (retry_after is not None and retry_after > 5):
                    raise ProviderError("rate_limited", True, retry_after,
                                        request_count=attempts) from None
                delay = float(retry_after) if retry_after is not None else 0.5 * (2 ** (attempts - 1))
                sleep(delay)

        try:
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise ValueError("data list required")
            retrieved_at = utc_now()
            papers, rejected = [], 0
            for row in rows[:max_results]:
                try:
                    papers.append(self._paper(row, retrieved_at))
                except (KeyError, TypeError, ValueError, UnicodeError):
                    rejected += 1
            return PaperBatch(tuple(papers), api_requests=attempts, rejected_papers=rejected,
                              providers_attempted=(self.name,))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ProviderError("invalid_response", True, request_count=attempts) from None
