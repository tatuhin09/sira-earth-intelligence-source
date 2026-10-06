"""Provider-neutral data contracts. A citation is not a truth judgment."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_url(value: str) -> str:
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Invalid URL")
    parts = urlsplit(value)
    if (parts.scheme not in ("https", "http") or not parts.hostname
            or parts.username is not None or parts.password is not None):
        raise ValueError("Expected an HTTP(S) URL without credentials")
    _ = parts.port  # Reject malformed ports without visiting the URL.
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    excerpt: str
    retrieved_at: str

    def __post_init__(self):
        if not all(isinstance(x, str) and x.strip() for x in
                   (self.title, self.url, self.excerpt, self.retrieved_at)):
            raise ValueError("Source fields must be nonempty strings")
        object.__setattr__(self, "url", canonical_url(self.url))


@dataclass(frozen=True)
class SearchBatch:
    sources: tuple[Source, ...]
    api_requests: int = 0
    credits: float | None = 0
    rejected_sources: int = 0


class ProviderError(Exception):
    def __init__(self, code: str, request_sent: bool = False, retry_after: int | None = None,
                 request_count: int | None = None):
        super().__init__(code)
        self.code = code
        self.request_sent = request_sent
        self.retry_after = retry_after
        default_count = int(bool(request_sent))
        if request_count is None:
            request_count = default_count
        if type(request_count) is not int or request_count < default_count:
            raise ValueError("request_count must be a nonnegative integer consistent with request_sent")
        self.request_count = request_count


class SearchProvider(Protocol):
    name: str
    cache_namespace: str

    def search(self, query: str, max_results: int) -> SearchBatch: ...


def parse_sources(rows: list) -> tuple[tuple[Source, ...], int]:
    sources, rejected = [], 0
    retrieved_at = utc_now()
    for row in rows:
        try:
            sources.append(Source(row["title"], row["url"], row["content"], retrieved_at))
        except (KeyError, TypeError, ValueError):
            rejected += 1
    return tuple(sources), rejected
