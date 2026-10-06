"""Cloud extraction only: no user-supplied URL is fetched by the local client."""
import json
from urllib.request import Request

from ..config import validate_key
from ..models import ProviderError, utc_now
from ..reading import Document, ExtractBatch, public_url
from .http_json import open_request, reported_credits, request_json


class TavilyExtract:
    name = "tavily_extract"
    cache_namespace = "tavily-extract-basic-text-v1"

    def __init__(self, key: str):
        self._key = validate_key(key)

    def read(self, urls: tuple[str, ...]) -> ExtractBatch:
        if len(urls) > 5:
            raise ValueError("Read at most five URLs per run")
        urls = tuple(dict.fromkeys(public_url(url) for url in urls))
        if not urls:
            return ExtractBatch((), {})
        body = {"urls": list(urls), "extract_depth": "basic", "format": "text",
                "include_images": False, "include_favicon": False,
                "timeout": 10, "include_usage": True}
        request = Request("https://api.tavily.com/extract", data=json.dumps(body).encode(),
                          method="POST", headers={"Authorization": f"Bearer {self._key}",
                          "Content-Type": "application/json", "User-Agent": "SIRA/0.2"})
        payload = request_json(request, 20, open_request)
        rows, failed = payload.get("results"), payload.get("failed_results", [])
        if not isinstance(rows, list) or not isinstance(failed, list):
            raise ProviderError("invalid_response", True)
        documents, failures, seen = {}, dict.fromkeys(urls, "missing_result"), set()
        for row in rows:
            try:
                url = public_url(row["url"])
            except (KeyError, TypeError, ValueError, UnicodeError):
                continue
            if url not in urls:
                continue
            if url in seen:
                documents.pop(url, None)
                failures[url] = "ambiguous_result"
                continue
            seen.add(url)
            try:
                doc = Document(url, row["raw_content"], utc_now())
            except (KeyError, TypeError, ValueError):
                failures[url] = "invalid_document"
                continue
            documents[url] = doc
            failures.pop(url, None)
        for row in failed:
            try:
                url = public_url(row["url"])
            except (KeyError, TypeError, ValueError, UnicodeError):
                continue
            if url in urls:
                failures[url] = "ambiguous_result" if url in seen else "extraction_failed"
                documents.pop(url, None)
        return ExtractBatch(tuple(documents.values()), failures, 1, reported_credits(payload))
