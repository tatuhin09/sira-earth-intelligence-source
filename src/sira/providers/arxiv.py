"""Bounded, dependency-free arXiv Atom metadata search."""
from http.client import HTTPException
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request
from xml.etree import ElementTree as ET

from ..models import ProviderError, utc_now
from ..papers import Paper, PaperBatch
from .http_json import open_request


_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_VERSION_SUFFIX = re.compile(r"v\d+$", re.IGNORECASE)
_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)


class ArxivProvider:
    name = "arxiv"
    cache_namespace = "arxiv-atom-relevance-v1"
    _endpoint = "https://export.arxiv.org/api/query"
    _max_bytes = 2 * 1024 * 1024

    def __init__(self, timeout: int = 20):
        if not 1 <= timeout <= 60:
            raise ValueError("timeout must be 1..60 seconds")
        self.timeout = timeout

    @staticmethod
    def _query(value: str) -> str:
        value = " ".join(value.split())
        if not value or len(value) > 500:
            raise ValueError("Question must contain 1..500 characters")
        terms = _WORD.findall(value)
        if not terms:
            raise ValueError("Question must contain searchable text")
        # Bound query grammar and prevent user text from becoming arXiv operators.
        terms = terms[:16]
        return " AND ".join("all:" + term for term in terms)

    @staticmethod
    def _read_xml(request: Request, timeout: int) -> bytes:
        try:
            with open_request(request, timeout=timeout) as response:
                data = response.read(ArxivProvider._max_bytes + 1)
        except HTTPError as error:
            retry = error.headers.get("Retry-After", "") if error.headers else ""
            code = error.code
            error.close()
            raise ProviderError(f"http_{code}", True,
                                int(retry) if retry.isdigit() and len(retry) <= 8 else None,
                                request_count=1) from None
        except (TimeoutError, URLError, OSError, HTTPException):
            raise ProviderError("network_or_timeout", True, request_count=1) from None
        if len(data) > ArxivProvider._max_bytes:
            raise ProviderError("response_too_large", True, request_count=1)
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise ProviderError("invalid_response", True, request_count=1)
        return data

    @staticmethod
    def _text(element: ET.Element, tag: str, *, required: bool = False) -> str | None:
        child = element.find(tag)
        text = " ".join("".join(child.itertext()).split()) if child is not None else ""
        if required and not text:
            raise ValueError("required Atom text missing")
        return text or None

    @staticmethod
    def _arxiv_id(raw_id: str) -> str:
        parts = urlsplit(raw_id)
        if parts.scheme not in ("http", "https") or parts.hostname not in ("arxiv.org", "www.arxiv.org"):
            raise ValueError("invalid arXiv id URL")
        marker = "/abs/"
        if marker not in parts.path:
            raise ValueError("invalid arXiv id path")
        value = parts.path.split(marker, 1)[1].strip("/")
        value = _VERSION_SUFFIX.sub("", value)
        if not value or len(value) > 100 or not re.fullmatch(r"[A-Za-z0-9.\-/]+", value):
            raise ValueError("invalid arXiv id")
        return value

    @staticmethod
    def _date(value: str | None) -> tuple[int | None, str | None]:
        if not isinstance(value, str) or len(value) < 10:
            return None, None
        date_text = value[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
            return None, None
        try:
            from datetime import date
            parsed = date.fromisoformat(date_text)
        except ValueError:
            return None, None
        return parsed.year, date_text

    @staticmethod
    def _paper(entry: ET.Element, retrieved_at: str) -> Paper:
        raw_id = ArxivProvider._text(entry, _ATOM + "id", required=True)
        title = ArxivProvider._text(entry, _ATOM + "title", required=True)
        abstract = ArxivProvider._text(entry, _ATOM + "summary")
        if abstract is not None and len(abstract) > 100_000:
            raise ValueError("abstract too large")
        arxiv_id = ArxivProvider._arxiv_id(raw_id)

        authors = []
        for author in entry.findall(_ATOM + "author")[:500]:
            name = ArxivProvider._text(author, _ATOM + "name")
            if name and len(name) <= 1000:
                authors.append(name)

        published = ArxivProvider._text(entry, _ATOM + "published")
        year, published_date = ArxivProvider._date(published)
        doi = ArxivProvider._text(entry, _ARXIV + "doi")
        if doi is not None:
            doi = doi.strip()
            if not doi or len(doi) > 1000 or any(ord(c) < 32 for c in doi):
                doi = None

        return Paper(
            paper_id="arxiv:" + arxiv_id,
            title=title,
            abstract=abstract,
            authors=tuple(authors),
            year=year,
            doi=doi,
            url="https://arxiv.org/abs/" + arxiv_id,
            open_access_pdf_url="https://arxiv.org/pdf/" + arxiv_id,
            retrieved_at=retrieved_at,
            provider="arxiv",
            published_date=published_date,
            providers=("arxiv",),
        )

    def search(self, query: str, max_results: int) -> PaperBatch:
        if not 1 <= max_results <= 3:
            raise ValueError("Paper max_results must be 1..3")
        params = urlencode({
            "search_query": self._query(query),
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
            "sortOrder": "descending",
        })
        request = Request(
            f"{self._endpoint}?{params}",
            method="GET",
            headers={"Accept": "application/atom+xml", "User-Agent": "SIRA/0.6"},
        )
        data = self._read_xml(request, self.timeout)
        try:
            root = ET.fromstring(data)
            if root.tag != _ATOM + "feed":
                raise ValueError("Atom feed required")
            retrieved_at = utc_now()
            papers = []
            rejected = 0
            for entry in root.findall(_ATOM + "entry")[:max_results]:
                try:
                    papers.append(self._paper(entry, retrieved_at))
                except (TypeError, ValueError, UnicodeError):
                    rejected += 1
            return PaperBatch(tuple(papers), api_requests=1, rejected_papers=rejected,
                              providers_attempted=(self.name,))
        except (ET.ParseError, TypeError, ValueError, UnicodeError, RecursionError):
            raise ProviderError("invalid_response", True, request_count=1) from None
