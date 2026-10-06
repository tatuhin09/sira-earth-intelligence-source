from __future__ import annotations
from urllib.parse import quote, urlencode
from urllib.request import Request
from ..models import ProviderError, canonical_url, utc_now
from ..papers import Paper, PaperBatch
from .http_json import open_request, request_json

class DOAJProvider:
    name = "doaj"
    cache_namespace = "doaj-current-article-search-v1"
    _endpoint = "https://doaj.org/api/search/articles/"

    def __init__(self, timeout: int = 20):
        if type(timeout) is not int or not 1 <= timeout <= 60:
            raise ValueError("timeout must be 1..60 seconds")
        self.timeout = timeout

    @staticmethod
    def _query(value: str) -> str:
        value = " ".join(str(value).split())
        if not value or len(value) > 500:
            raise ValueError("Question must contain 1..500 characters")
        return value

    @staticmethod
    def _text(value, maximum: int, required: bool = False):
        if not isinstance(value, str):
            if required:
                raise ValueError("required text missing")
            return None
        value = " ".join(value.split())
        if not value or len(value) > maximum:
            if required:
                raise ValueError("required text invalid")
            return None
        return value

    @staticmethod
    def _year(value):
        if type(value) is int and 1 <= value <= 9999:
            return value
        if isinstance(value, str) and value.isdigit():
            n = int(value)
            return n if 1 <= n <= 9999 else None
        return None

    @classmethod
    def _authors(cls, bib):
        rows = bib.get("author")
        if not isinstance(rows, list):
            return ()
        out = []
        seen = set()
        for row in rows[:500]:
            name = row.get("name") if isinstance(row, dict) else None
            clean = cls._text(name, 1000)
            if clean and clean.casefold() not in seen:
                seen.add(clean.casefold())
                out.append(clean)
        return tuple(out)

    @classmethod
    def _doi(cls, bib):
        rows = bib.get("identifier")
        if not isinstance(rows, list):
            return None
        for row in rows[:100]:
            if not isinstance(row, dict):
                continue
            if str(row.get("type", "")).casefold() == "doi":
                return cls._text(row.get("id"), 1000)
        return None

    @classmethod
    def _pdf(cls, bib):
        rows = bib.get("link")
        if not isinstance(rows, list):
            return None
        for row in rows[:100]:
            if not isinstance(row, dict):
                continue
            if (
                str(row.get("type", "")).casefold() == "fulltext"
                and str(row.get("content_type", "")).casefold() == "pdf"
                and isinstance(row.get("url"), str)
            ):
                try:
                    return canonical_url(row["url"].strip())
                except ValueError:
                    pass
        return None

    @classmethod
    def _paper(cls, row, retrieved_at: str):
        if not isinstance(row, dict):
            raise ValueError("DOAJ row must be object")
        rid = cls._text(row.get("id"), 128, True)
        if not all(c.isalnum() or c in "_-" for c in rid):
            raise ValueError("invalid DOAJ record id")
        bib = row.get("bibjson")
        if not isinstance(bib, dict):
            raise ValueError("bibjson required")
        title = cls._text(bib.get("title"), 4000, True)
        return Paper(
            paper_id="doaj:" + rid,
            title=title,
            abstract=cls._text(bib.get("abstract"), 100_000),
            authors=cls._authors(bib),
            year=cls._year(bib.get("year")),
            doi=cls._doi(bib),
            url="https://doaj.org/article/" + quote(rid, safe=""),
            open_access_pdf_url=cls._pdf(bib),
            retrieved_at=retrieved_at,
            provider=cls.name,
            providers=(cls.name,),
        )

    def search(self, query: str, max_results: int):
        if type(max_results) is not int or not 1 <= max_results <= 3:
            raise ValueError("Paper max_results must be 1..3")
        encoded = quote(self._query(query), safe="")
        request = Request(
            f"{self._endpoint}{encoded}?{urlencode({'pageSize': max_results})}",
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "SIRA/1.7D Knowledge Mesh",
            },
        )
        try:
            payload = request_json(
                request, self.timeout, open_request, max_bytes=2 * 1024 * 1024
            )
        except ProviderError as exc:
            exc.request_count = max(1, exc.request_count)
            raise
        try:
            rows = payload.get("results")
            if not isinstance(rows, list):
                raise ValueError("results list required")
            now = utc_now()
            papers = []
            rejected = 0
            for row in rows[:max_results]:
                try:
                    papers.append(self._paper(row, now))
                except (TypeError, ValueError, UnicodeError):
                    rejected += 1
            return PaperBatch(
                tuple(papers),
                api_requests=1,
                rejected_papers=rejected,
                providers_attempted=(self.name,),
            )
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ProviderError("invalid_response", True, request_count=1) from None
