"""One bounded REST request. No automatic retries, redirects or generated answer."""
import json
from urllib.request import Request

from ..config import validate_key
from ..models import ProviderError, SearchBatch, parse_sources
from .http_json import open_request, reported_credits, request_json


class TavilyProvider:
    name = "tavily"
    cache_namespace = "tavily-basic-general-v1"

    def __init__(self, key: str, timeout: int = 20):
        self._key = validate_key(key)
        self.timeout = timeout

    def search(self, query: str, max_results: int) -> SearchBatch:
        payload = {
            "query": query, "topic": "general", "search_depth": "basic",
            "auto_parameters": False, "max_results": max_results,
            "include_answer": False, "include_raw_content": False,
            "include_images": False, "include_usage": True,
        }
        request = Request("https://api.tavily.com/search",
                          data=json.dumps(payload).encode("utf-8"), method="POST",
                          headers={"Authorization": f"Bearer {self._key}",
                                   "Content-Type": "application/json", "User-Agent": "SIRA/0.1"})
        payload = request_json(request, self.timeout, open_request)
        try:
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise ValueError("Invalid results")
            sources, rejected = parse_sources(payload["results"][:max_results])
            credits = reported_credits(payload)
        except (ValueError, TypeError, UnicodeError):
            raise ProviderError("invalid_response", True) from None
        return SearchBatch(sources, api_requests=1, credits=credits, rejected_sources=rejected)
