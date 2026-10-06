"""Free public Crossref paper-metadata search used as a resilient fallback."""
from html.parser import HTMLParser
from urllib.parse import urlencode
from urllib.request import Request

from ..models import ProviderError, canonical_url, utc_now
from ..papers import Paper, PaperBatch
from .http_json import open_request, request_json


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)


def _plain_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, UnicodeError):
        return None
    text = " ".join("".join(parser.parts).split())
    return text if text and len(text) <= 100_000 else None


class CrossrefProvider:
    name = "crossref"
    cache_namespace = "crossref-works-bibliographic-v1"
    _endpoint = "https://api.crossref.org/works"
    _select = "DOI,title,author,published,published-print,published-online,URL,abstract,type"

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
    def _title(row: dict) -> str:
        value = row.get("title")
        if isinstance(value, list) and value and isinstance(value[0], str):
            title = " ".join(value[0].split())
        elif isinstance(value, str):
            title = " ".join(value.split())
        else:
            title = ""
        if not title or len(title) > 4000:
            raise ValueError("valid title required")
        return title

    @staticmethod
    def _authors(row: dict) -> tuple[str, ...]:
        raw = row.get("author")
        if raw is None:
            return ()
        if not isinstance(raw, list):
            return ()
        names: list[str] = []
        for author in raw[:500]:
            if not isinstance(author, dict):
                continue
            name = author.get("name")
            if not isinstance(name, str) or not name.strip():
                pieces = [author.get("given"), author.get("family")]
                name = " ".join(piece.strip() for piece in pieces if isinstance(piece, str) and piece.strip())
            name = " ".join(name.split()) if isinstance(name, str) else ""
            if name and len(name) <= 1000:
                names.append(name)
        return tuple(names)

    @staticmethod
    def _year(row: dict) -> int | None:
        for field in ("published-print", "published-online", "published"):
            value = row.get(field)
            parts = value.get("date-parts") if isinstance(value, dict) else None
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                year = parts[0][0]
                if type(year) is int and 1 <= year <= 9999:
                    return year
        return None

    @staticmethod
    def _paper(row: dict, retrieved_at: str) -> Paper:
        if not isinstance(row, dict):
            raise ValueError("paper row must be an object")
        doi = row.get("DOI")
        if not isinstance(doi, str):
            raise ValueError("DOI required")
        doi = doi.strip()
        if not doi or len(doi) > 1000 or any(ord(c) < 32 for c in doi):
            raise ValueError("valid DOI required")
        url = row.get("URL")
        if not isinstance(url, str) or not url.strip():
            url = "https://doi.org/" + doi
        return Paper(
            paper_id="crossref:" + doi.casefold(),
            title=CrossrefProvider._title(row),
            abstract=_plain_text(row.get("abstract")),
            authors=CrossrefProvider._authors(row),
            year=CrossrefProvider._year(row),
            doi=doi,
            url=url,
            open_access_pdf_url=None,
            retrieved_at=retrieved_at,
            provider="crossref",
        )

    def search(self, query: str, max_results: int) -> PaperBatch:
        if not 1 <= max_results <= 3:
            raise ValueError("Paper max_results must be 1..3")
        params = urlencode({
            "query.bibliographic": self._query(query),
            "rows": max_results,
            "select": self._select,
        })
        request = Request(
            f"{self._endpoint}?{params}",
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "SIRA/0.6"},
        )
        try:
            payload = request_json(request, self.timeout, open_request)
        except ProviderError as exc:
            exc.request_count = max(1, exc.request_count)
            raise
        try:
            message = payload.get("message")
            rows = message.get("items") if isinstance(message, dict) else None
            if not isinstance(rows, list):
                raise ValueError("items list required")
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
