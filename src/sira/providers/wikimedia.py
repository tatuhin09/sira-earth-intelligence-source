from __future__ import annotations
from dataclasses import asdict, dataclass
from urllib.parse import urlencode, quote
from urllib.request import Request
from ..models import ProviderError, utc_now
from .http_json import open_request, request_json

@dataclass(frozen=True, slots=True)
class KnowledgeRecord:
    record_id: str
    title: str
    description: str | None
    url: str
    language: str
    retrieved_at: str
    provider: str = "wikimedia"

    def to_dict(self):
        return asdict(self)

@dataclass(frozen=True, slots=True)
class KnowledgeBatch:
    records: tuple[KnowledgeRecord, ...]
    api_requests: int
    rejected_records: int = 0
    providers_attempted: tuple[str, ...] = ()

class WikimediaProvider:
    name = "wikimedia"
    cache_namespace = "wikimedia-en-page-search-v1"
    _endpoint = "https://en.wikipedia.org/w/rest.php/v1/search/page"

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

    @classmethod
    def _record(cls, row, retrieved_at: str):
        if not isinstance(row, dict):
            raise ValueError("Wikimedia result must be object")
        page_id = row.get("id")
        if type(page_id) is not int or page_id < 0:
            raise ValueError("valid page id required")
        key = cls._text(row.get("key"), 1000, True)
        title = cls._text(row.get("title"), 1000, True)
        desc = cls._text(row.get("description"), 2000, False)
        return KnowledgeRecord(
            record_id=f"wikimedia:en:{page_id}",
            title=title,
            description=desc,
            url="https://en.wikipedia.org/wiki/" + quote(key, safe=""),
            language="en",
            retrieved_at=retrieved_at,
        )

    def search(self, query: str, max_results: int):
        if type(max_results) is not int or not 1 <= max_results <= 3:
            raise ValueError("Knowledge max_results must be 1..3")
        params = urlencode({"q": self._query(query), "limit": max_results})
        request = Request(
            f"{self._endpoint}?{params}",
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "SIRA/1.7D Knowledge Mesh",
            },
        )
        try:
            payload = request_json(
                request, self.timeout, open_request, max_bytes=1024 * 1024
            )
        except ProviderError as exc:
            exc.request_count = max(1, exc.request_count)
            raise
        try:
            rows = payload.get("pages")
            if not isinstance(rows, list):
                raise ValueError("pages list required")
            now = utc_now()
            records = []
            rejected = 0
            for row in rows[:max_results]:
                try:
                    records.append(self._record(row, now))
                except (TypeError, ValueError, UnicodeError):
                    rejected += 1
            return KnowledgeBatch(
                tuple(records), 1, rejected, (self.name,)
            )
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ProviderError("invalid_response", True, request_count=1) from None
