"""Offline replay of synthetic retrieval inputs, never represented as live search."""
import hashlib
import json
from pathlib import Path

from ..models import ProviderError, SearchBatch, parse_sources


class FixtureProvider:
    name = "fixture"

    def __init__(self, path: Path):
        data = path.read_bytes()
        self.cache_namespace = "fixture-" + hashlib.sha256(data).hexdigest()
        self.cases = json.loads(data)["cases"]

    def search(self, query: str, max_results: int) -> SearchBatch:
        for case in self.cases:
            if case["query"] == query:
                sources, rejected = parse_sources(case["results"][:max_results])
                return SearchBatch(sources, rejected_sources=rejected)
        raise ProviderError("fixture_query_not_found")
