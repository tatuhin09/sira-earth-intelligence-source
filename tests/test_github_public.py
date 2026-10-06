from __future__ import annotations

from email.message import Message
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sira.github_public import (
    GitHubPublicClient,
    GitHubRateLimitError,
    GitHubResponseError,
)


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None):
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self._body = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        pass


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("unexpected network request")
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class GitHubPublicClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache"

    def tearDown(self):
        os.environ.pop("SIRA_GITHUB_TOKEN", None)
        self.tmp.cleanup()

    def test_search_is_get_only_and_uses_versioned_api_headers(self):
        transport = ScriptedTransport([
            FakeResponse(
                json.dumps({"total_count": 1, "items": [{"full_name": "openai/example"}]}).encode(),
                headers={
                    "etag": '"abc"',
                    "x-ratelimit-limit": "60",
                    "x-ratelimit-remaining": "59",
                    "x-ratelimit-reset": "2000000000",
                    "x-ratelimit-resource": "search",
                },
            )
        ])
        client = GitHubPublicClient(self.cache, transport=transport)
        result = client.search_repositories("agent memory", per_page=5)
        request, timeout = transport.requests[0]

        self.assertEqual(request.get_method(), "GET")
        self.assertIn("/search/repositories?", request.full_url)
        self.assertIn("q=agent+memory", request.full_url)
        self.assertEqual(request.headers["X-github-api-version"], "2022-11-28")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(result.data["total_count"], 1)
        self.assertEqual(result.rate_limit.remaining, 59)
        self.assertFalse(result.from_cache)

    def test_fresh_cache_avoids_second_network_call(self):
        clock = [1000.0]
        transport = ScriptedTransport([
            FakeResponse(b'{"full_name":"owner/repo"}', headers={"etag": '"v1"'})
        ])
        client = GitHubPublicClient(
            self.cache,
            transport=transport,
            clock=lambda: clock[0],
            cache_ttl_seconds=3600,
        )
        first = client.get_repository("owner", "repo")
        clock[0] += 10
        second = client.get_repository("owner", "repo")

        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(len(transport.requests), 1)

    def test_stale_cache_uses_etag_and_304(self):
        clock = [1000.0]
        first_transport = ScriptedTransport([
            FakeResponse(b'{"full_name":"owner/repo"}', headers={"etag": '"v1"'})
        ])
        client = GitHubPublicClient(
            self.cache,
            transport=first_transport,
            clock=lambda: clock[0],
            cache_ttl_seconds=1,
        )
        client.get_repository("owner", "repo")
        clock[0] += 2

        headers = Message()
        headers["x-ratelimit-remaining"] = "58"
        http_304 = HTTPError(
            "https://api.github.com/repos/owner/repo",
            304,
            "Not Modified",
            headers,
            None,
        )
        self.addCleanup(http_304.close)
        second_transport = ScriptedTransport([http_304])
        client._transport = second_transport
        result = client.get_repository("owner", "repo")

        request, _ = second_transport.requests[0]
        self.assertEqual(request.headers["If-none-match"], '"v1"')
        self.assertTrue(result.from_cache)
        self.assertEqual(result.data["full_name"], "owner/repo")

    def test_rate_limit_error_exposes_wait_metadata_without_retry(self):
        headers = Message()
        headers["x-ratelimit-remaining"] = "0"
        headers["x-ratelimit-reset"] = "2000000000"
        headers["x-ratelimit-resource"] = "core"
        err = HTTPError(
            "https://api.github.com/repos/owner/repo",
            403,
            "Forbidden",
            headers,
            None,
        )
        self.addCleanup(err.close)
        transport = ScriptedTransport([err])
        client = GitHubPublicClient(self.cache, transport=transport)

        with self.assertRaises(GitHubRateLimitError) as ctx:
            client.get_repository("owner", "repo")

        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.remaining, 0)
        self.assertEqual(ctx.exception.reset_at, 2000000000)
        self.assertEqual(len(transport.requests), 1)

    def test_optional_token_is_injected_only_in_request_header(self):
        os.environ["SIRA_GITHUB_TOKEN"] = "secret-test-token"
        transport = ScriptedTransport([FakeResponse(b'{"full_name":"owner/repo"}')])
        client = GitHubPublicClient(self.cache, transport=transport)
        client.get_repository("owner", "repo")
        request, _ = transport.requests[0]

        self.assertEqual(request.headers["Authorization"], "Bearer secret-test-token")
        cache_text = "".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in self.cache.glob("*.json")
        )
        self.assertNotIn("secret-test-token", cache_text)

    def test_text_file_never_follows_download_url_and_enforces_size_limit(self):
        transport = ScriptedTransport([
            FakeResponse(
                b"x" * 17,
                headers={"content-length": "17"},
            )
        ])
        client = GitHubPublicClient(
            self.cache,
            transport=transport,
            max_text_bytes=16,
        )
        with self.assertRaises(GitHubResponseError):
            client.get_text_file("owner", "repo", "src/main.py")
        request, _ = transport.requests[0]
        self.assertTrue(request.full_url.startswith("https://api.github.com/repos/owner/repo/contents/"))

    def test_unsafe_repo_paths_are_rejected_before_network(self):
        transport = ScriptedTransport([])
        client = GitHubPublicClient(self.cache, transport=transport)

        for path in ("../secret", "/etc/passwd", "src/../secret", r"src\secret"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    client.get_text_file("owner", "repo", path)
        self.assertEqual(transport.requests, [])

    def test_rate_limit_endpoint_bypasses_cache(self):
        transport = ScriptedTransport([
            FakeResponse(b'{"resources":{"core":{"remaining":60}}}'),
            FakeResponse(b'{"resources":{"core":{"remaining":59}}}'),
        ])
        client = GitHubPublicClient(self.cache, transport=transport)
        first = client.rate_limit()
        second = client.rate_limit()
        self.assertEqual(first.data["resources"]["core"]["remaining"], 60)
        self.assertEqual(second.data["resources"]["core"]["remaining"], 59)
        self.assertEqual(len(transport.requests), 2)


if __name__ == "__main__":
    unittest.main()
