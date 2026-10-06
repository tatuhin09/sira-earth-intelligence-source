"""Anonymous/free-start OpenAlex scholarly metadata fallback for opportunity research.

This provider intentionally sends no API key or billing credential. It is for bounded
fallback discovery only; higher-scale keyed usage can be designed separately.
"""
from __future__ import annotations

from datetime import date
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from ..models import ProviderError, canonical_url, utc_now
from ..papers import Paper, PaperBatch
from .http_json import open_request, request_json


_OPENALEX_ID_RE = re.compile(r"W[0-9]+\Z")
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "http://dx.doi.org/", "https://dx.doi.org/")


class OpenAlexProvider:
    name = "openalex"
    cache_namespace = "openalex-works-search-anonymous-v1"
    _endpoint = "https://api.openalex.org/works"
    _select = (
        "id,title,doi,publication_year,publication_date,authorships,"
        "abstract_inverted_index,primary_location,best_oa_location"
    )

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
    def _work_id(value) -> tuple[str, str]:
        if not isinstance(value, str):
            raise ValueError("OpenAlex work id required")
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.hostname != "openalex.org":
            raise ValueError("valid OpenAlex work URL required")
        work_id = parts.path.strip("/")
        if not _OPENALEX_ID_RE.fullmatch(work_id):
            raise ValueError("valid OpenAlex work id required")
        return work_id, f"https://openalex.org/{work_id}"

    @staticmethod
    def _title(row: dict) -> str:
        value = row.get("title")
        title = " ".join(value.split()) if isinstance(value, str) else ""
        if not title or len(title) > 4000:
            raise ValueError("valid title required")
        return title

    @staticmethod
    def _doi(value) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        doi = value.strip()
        folded = doi.casefold()
        for prefix in _DOI_PREFIXES:
            if folded.startswith(prefix):
                doi = doi[len(prefix):]
                break
        if not doi or len(doi) > 1000 or any(ord(char) < 32 for char in doi):
            return None
        return doi

    @staticmethod
    def _year(value) -> int | None:
        return value if type(value) is int and 1 <= value <= 9999 else None

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
        raw = row.get("authorships")
        if not isinstance(raw, list):
            return ()
        names: list[str] = []
        seen: set[str] = set()
        for authorship in raw[:500]:
            author = authorship.get("author") if isinstance(authorship, dict) else None
            name = author.get("display_name") if isinstance(author, dict) else None
            name = " ".join(name.split()) if isinstance(name, str) else ""
            if not name or len(name) > 1000:
                continue
            folded = name.casefold()
            if folded not in seen:
                seen.add(folded)
                names.append(name)
        return tuple(names)

    @staticmethod
    def _abstract(value) -> str | None:
        if value is None:
            return None
        if not isinstance(value, dict) or len(value) > 50_000:
            return None
        positions: dict[int, str] = {}
        max_position = -1
        for token, raw_positions in value.items():
            if not isinstance(token, str) or not token or len(token) > 500 or not isinstance(raw_positions, list):
                return None
            for position in raw_positions[:10_000]:
                if type(position) is not int or not 0 <= position <= 100_000:
                    return None
                if position in positions:
                    return None
                positions[position] = token
                max_position = max(max_position, position)
        if max_position < 0:
            return None
        if max_position + 1 != len(positions):
            return None
        text = " ".join(positions[index] for index in range(max_position + 1))
        return text if text and len(text) <= 100_000 else None

    @staticmethod
    def _pdf_url(row: dict) -> str | None:
        for key in ("best_oa_location", "primary_location"):
            location = row.get(key)
            value = location.get("pdf_url") if isinstance(location, dict) else None
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                return canonical_url(value.strip())
            except ValueError:
                continue
        return None

    @classmethod
    def _paper(cls, row: dict, retrieved_at: str) -> Paper:
        if not isinstance(row, dict):
            raise ValueError("work row must be an object")
        work_id, work_url = cls._work_id(row.get("id"))
        return Paper(
            paper_id="openalex:" + work_id,
            title=cls._title(row),
            abstract=cls._abstract(row.get("abstract_inverted_index")),
            authors=cls._authors(row),
            year=cls._year(row.get("publication_year")),
            doi=cls._doi(row.get("doi")),
            url=work_url,
            open_access_pdf_url=cls._pdf_url(row),
            retrieved_at=retrieved_at,
            provider=cls.name,
            published_date=cls._published_date(row.get("publication_date")),
            providers=(cls.name,),
        )

    def search(self, query: str, max_results: int) -> PaperBatch:
        if not 1 <= max_results <= 3:
            raise ValueError("Paper max_results must be 1..3")
        params = urlencode({
            "search": self._query(query),
            "per_page": max_results,
            "select": self._select,
        })
        request = Request(
            f"{self._endpoint}?{params}",
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "SIRA/1.0"},
        )
        try:
            payload = request_json(request, self.timeout, open_request)
        except ProviderError as exc:
            exc.request_count = max(1, exc.request_count)
            raise
        try:
            rows = payload.get("results")
            if not isinstance(rows, list):
                raise ValueError("results list required")
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
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ProviderError("invalid_response", True, request_count=1) from None
