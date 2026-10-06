"""Bounded public PDF reading backed by the shared SIRA text extractor."""
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request

from ..models import ProviderError, utc_now
from ..pdf_extraction import PdfExtractionError, extract_pdf_text as _extract_pdf_text
from ..reading import Document, ExtractBatch, public_url
from .http_json import open_request


_MAX_PDF_BYTES = 8 * 1024 * 1024
_MAX_TEXT_CHARS = 200_000


def extract_pdf_text(data: bytes) -> str:
    return _extract_pdf_text(data, max_bytes=_MAX_PDF_BYTES, max_chars=_MAX_TEXT_CHARS)


class PdfTextReader:
    name = "bounded_pdf_text"
    cache_namespace = "bounded-pdf-text-v2"

    def __init__(self, timeout: int = 20):
        if not 1 <= timeout <= 60:
            raise ValueError("timeout must be 1..60 seconds")
        self.timeout = timeout

    def _one(self, url: str) -> Document:
        safe = public_url(url)
        request = Request(safe, method="GET", headers={
            "Accept": "application/pdf",
            "User-Agent": "SIRA/0.7",
        })
        try:
            with open_request(request, timeout=self.timeout) as response:
                data = response.read(_MAX_PDF_BYTES + 1)
        except HTTPError as error:
            code = error.code
            error.close()
            raise ProviderError(f"http_{code}", True, request_count=1) from None
        except (TimeoutError, URLError, OSError, HTTPException):
            raise ProviderError("network_or_timeout", True, request_count=1) from None
        if len(data) > _MAX_PDF_BYTES:
            raise ProviderError("pdf_too_large", True, request_count=1)
        try:
            text = _extract_pdf_text(data, max_bytes=_MAX_PDF_BYTES,
                                    max_chars=_MAX_TEXT_CHARS, timeout=self.timeout)
        except PdfExtractionError as exc:
            raise ProviderError(exc.code, True, request_count=1) from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ProviderError("invalid_pdf", True, request_count=1) from None
        return Document(safe, text, utc_now())

    def read(self, urls: tuple[str, ...]) -> ExtractBatch:
        if len(urls) > 3:
            raise ValueError("PDF reader accepts at most three URLs")
        documents, failures = [], {}
        api_requests = 0
        seen = set()
        for raw in urls:
            if raw in seen:
                continue
            seen.add(raw)
            try:
                safe = public_url(raw)
            except (ValueError, UnicodeError):
                failures[raw] = "unsafe_url"
                continue
            try:
                document = self._one(safe)
                documents.append(document)
                api_requests += 1
            except ProviderError as exc:
                api_requests += exc.request_count
                failures[safe] = exc.code
        return ExtractBatch(tuple(documents), failures, api_requests=api_requests, credits=0)
