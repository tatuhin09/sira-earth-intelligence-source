"""Bounded public Europe PMC biomedical literature search."""
from __future__ import annotations

from datetime import date
import re
from urllib.parse import urlencode
from urllib.request import Request

from ..models import ProviderError, utc_now
from ..papers import Paper, PaperBatch
from .http_json import open_request, request_json


class EuropePMCProvider:
    name = "europe_pmc"
    cache_namespace = "europe-pmc-core-search-v1"
    _endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    def __init__(self, timeout: int = 20):
        if not 1 <= timeout <= 60:
            raise ValueError("timeout must be 1..60 seconds")
        self.timeout = timeout

    @staticmethod
    def _query(value: str) -> str:
        value = " ".join(value.split())
        if not value or len(value) > 500:
            raise ValueError("Question must contain 1..500 characters")
        return value

    @staticmethod
    def _clean_text(value, *, maximum: int) -> str | None:
        if not isinstance(value, str):
            return None
        text = " ".join(value.split())
        if not text or len(text) > maximum:
            return None
        return text

    @staticmethod
    def _year(value) -> int | None:
        if isinstance(value, str) and re.fullmatch(r"\d{4}", value):
            year = int(value)
            return year if 1 <= year <= 9999 else None
        if type(value) is int and 1 <= value <= 9999:
            return value
        return None

    @staticmethod
    def _published_date(value) -> str | None:
        if not isinstance(value, str) or len(value) != 10:
            return None
        try:
            date.fromisoformat(value)
        except ValueError:
            return None
        return value

    @staticmethod
    def _authors(row: dict) -> tuple[str, ...]:
        author_list = row.get("authorList")
        rows = author_list.get("author") if isinstance(author_list, dict) else None
        names: list[str] = []
        if isinstance(rows, list):
            for author in rows[:500]:
                name = author.get("fullName") if isinstance(author, dict) else None
                name = " ".join(name.split()) if isinstance(name, str) else ""
                if name and len(name) <= 1000:
                    names.append(name)
        if names:
            return tuple(names)

        author_string = row.get("authorString")
        if isinstance(author_string, str):
            author_string = " ".join(author_string.split())
            if author_string and len(author_string) <= 20_000:
                return tuple(
                    piece.strip()
                    for piece in author_string.split(",")
                    if piece.strip()
                )[:500]
        return ()

    @classmethod
    def _paper(cls, row: dict, retrieved_at: str) -> Paper:
        if not isinstance(row, dict):
            raise ValueError("Europe PMC row must be an object")

        source = cls._clean_text(row.get("source"), maximum=16)
        identifier = cls._clean_text(row.get("id"), maximum=128)
        title = cls._clean_text(row.get("title"), maximum=4000)
        if not source or not re.fullmatch(r"[A-Za-z0-9_-]+", source):
            raise ValueError("valid Europe PMC source required")
        if not identifier or not re.fullmatch(r"[A-Za-z0-9._-]+", identifier):
            raise ValueError("valid Europe PMC identifier required")
        if not title:
            raise ValueError("valid title required")

        doi = cls._clean_text(row.get("doi"), maximum=1000)
        abstract = cls._clean_text(row.get("abstractText"), maximum=100_000)

        return Paper(
            paper_id=f"europe_pmc:{source}:{identifier}",
            title=title,
            abstract=abstract,
            authors=cls._authors(row),
            year=cls._year(row.get("pubYear")),
            doi=doi,
            url=f"https://europepmc.org/article/{source}/{identifier}",
            open_access_pdf_url=None,
            retrieved_at=retrieved_at,
            provider=cls.name,
            published_date=cls._published_date(row.get("firstPublicationDate")),
            providers=(cls.name,),
        )

    def search(self, query: str, max_results: int) -> PaperBatch:
        if not 1 <= max_results <= 3:
            raise ValueError("Paper max_results must be 1..3")
        params = urlencode({
            "query": self._query(query),
            "pageSize": max_results,
            "resultType": "core",
            "format": "json",
        })
        request = Request(
            f"{self._endpoint}?{params}",
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "SIRA/1.7 Research Mesh",
            },
        )
        try:
            payload = request_json(request, self.timeout, open_request)
        except ProviderError as exc:
            exc.request_count = max(1, exc.request_count)
            raise

        try:
            result_list = payload.get("resultList")
            rows = result_list.get("result") if isinstance(result_list, dict) else None
            if not isinstance(rows, list):
                raise ValueError("result list required")
            retrieved_at = utc_now()
            papers: list[Paper] = []
            rejected = 0
            for row in rows[:max_results]:
                try:
                    papers.append(self._paper(row, retrieved_at))
                except (KeyError, TypeError, ValueError, UnicodeError):
                    rejected += 1
            return PaperBatch(
                tuple(papers),
                api_requests=1,
                rejected_papers=rejected,
                providers_attempted=(self.name,),
            )
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ProviderError("invalid_response", True, request_count=1) from None
