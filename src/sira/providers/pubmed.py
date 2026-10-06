"""Bounded PubMed E-utilities metadata search with responsible local throttling."""
from __future__ import annotations

import re
from time import monotonic, sleep
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request

from ..models import ProviderError, utc_now
from ..papers import Paper, PaperBatch
from .http_json import open_request, request_json


class PubMedProvider:
    name = "pubmed"
    cache_namespace = "pubmed-eutils-summary-v1"
    _search_endpoint = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    _summary_endpoint = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

    def __init__(
        self,
        timeout: int = 20,
        *,
        api_key: str | None = None,
        contact_email: str | None = None,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ):
        if not 1 <= timeout <= 60:
            raise ValueError("timeout must be 1..60 seconds")
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
            raise ValueError("api_key must be nonempty text or null")
        if contact_email is not None:
            if (
                not isinstance(contact_email, str)
                or not contact_email.strip()
                or len(contact_email.strip()) > 320
                or "@" not in contact_email
            ):
                raise ValueError("contact_email must be a bounded email address or null")
        self.timeout = timeout
        self.api_key = api_key.strip() if isinstance(api_key, str) else None
        self.contact_email = contact_email.strip() if isinstance(contact_email, str) else None
        self._clock = clock
        self._sleeper = sleeper
        self._last_request_at: float | None = None

    @staticmethod
    def _query(value: str) -> str:
        value = " ".join(value.split())
        if not value or len(value) > 500:
            raise ValueError("Question must contain 1..500 characters")
        return value

    def _throttle(self) -> None:
        interval = 0.11 if self.api_key else 0.34
        now = self._clock()
        if self._last_request_at is not None:
            remaining = interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleeper(remaining)
                now = self._clock()
        self._last_request_at = now

    def _common_params(self) -> dict[str, str]:
        params = {"tool": "SIRA"}
        if self.api_key:
            params["api_key"] = self.api_key
        if self.contact_email:
            params["email"] = self.contact_email
        return params

    def _json(self, endpoint: str, params: dict[str, str | int]) -> dict:
        merged: dict[str, str | int] = {**self._common_params(), **params}
        request = Request(
            endpoint + "?" + urlencode(merged),
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "SIRA/1.7 Research Mesh",
            },
        )
        self._throttle()
        return request_json(request, self.timeout, open_request)

    @staticmethod
    def _title(row: dict) -> str:
        value = row.get("title")
        title = " ".join(value.split()) if isinstance(value, str) else ""
        if not title or len(title) > 4000:
            raise ValueError("valid title required")
        return title

    @staticmethod
    def _authors(row: dict) -> tuple[str, ...]:
        raw = row.get("authors")
        if not isinstance(raw, list):
            return ()
        names: list[str] = []
        for author in raw[:500]:
            name = author.get("name") if isinstance(author, dict) else None
            name = " ".join(name.split()) if isinstance(name, str) else ""
            if name and len(name) <= 1000:
                names.append(name)
        return tuple(names)

    @staticmethod
    def _year(row: dict) -> int | None:
        value = row.get("pubdate")
        if not isinstance(value, str):
            return None
        match = re.search(r"\b(\d{4})\b", value)
        if not match:
            return None
        year = int(match.group(1))
        return year if 1 <= year <= 9999 else None

    @staticmethod
    def _doi(row: dict) -> str | None:
        article_ids = row.get("articleids")
        if not isinstance(article_ids, list):
            return None
        for item in article_ids:
            if not isinstance(item, dict):
                continue
            if str(item.get("idtype", "")).casefold() != "doi":
                continue
            value = item.get("value")
            if isinstance(value, str):
                value = value.strip()
                if value and len(value) <= 1000 and not any(ord(c) < 32 for c in value):
                    return value
        return None

    @classmethod
    def _paper(cls, pmid: str, row: dict, retrieved_at: str) -> Paper:
        if not re.fullmatch(r"\d{1,20}", pmid):
            raise ValueError("valid PMID required")
        if not isinstance(row, dict):
            raise ValueError("PubMed summary row must be an object")
        return Paper(
            paper_id="pubmed:" + pmid,
            title=cls._title(row),
            abstract=None,
            authors=cls._authors(row),
            year=cls._year(row),
            doi=cls._doi(row),
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            open_access_pdf_url=None,
            retrieved_at=retrieved_at,
            provider=cls.name,
            providers=(cls.name,),
        )

    def search(self, query: str, max_results: int) -> PaperBatch:
        if not 1 <= max_results <= 3:
            raise ValueError("Paper max_results must be 1..3")

        requests = 0
        try:
            search_payload = self._json(
                self._search_endpoint,
                {
                    "db": "pubmed",
                    "retmode": "json",
                    "retmax": max_results,
                    "term": self._query(query),
                },
            )
            requests += 1
        except ProviderError as exc:
            exc.request_count = max(1, exc.request_count)
            raise

        try:
            esearch = search_payload.get("esearchresult")
            ids = esearch.get("idlist") if isinstance(esearch, dict) else None
            if not isinstance(ids, list):
                raise ValueError("PubMed id list required")
            pmids = [
                value for value in ids[:max_results]
                if isinstance(value, str) and re.fullmatch(r"\d{1,20}", value)
            ]
        except (TypeError, ValueError, RecursionError):
            raise ProviderError("invalid_response", True, request_count=requests) from None

        if not pmids:
            return PaperBatch(
                (),
                api_requests=requests,
                rejected_papers=0,
                providers_attempted=(self.name,),
            )

        try:
            summary_payload = self._json(
                self._summary_endpoint,
                {
                    "db": "pubmed",
                    "retmode": "json",
                    "id": ",".join(pmids),
                },
            )
            requests += 1
        except ProviderError as exc:
            exc.request_count = max(requests + 1, exc.request_count)
            raise

        try:
            result = summary_payload.get("result")
            if not isinstance(result, dict):
                raise ValueError("PubMed summary result required")
            retrieved_at = utc_now()
            papers: list[Paper] = []
            rejected = 0
            for pmid in pmids:
                try:
                    papers.append(self._paper(pmid, result.get(pmid), retrieved_at))
                except (KeyError, TypeError, ValueError, UnicodeError):
                    rejected += 1
            return PaperBatch(
                tuple(papers),
                api_requests=requests,
                rejected_papers=rejected,
                providers_attempted=(self.name,),
            )
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ProviderError("invalid_response", True, request_count=requests) from None
