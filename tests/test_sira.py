"""Behavioral regression tests; no network access or real API key required."""
import io
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.config import Settings, load_key
from sira.controller import research
from sira.models import ProviderError
from sira.providers.fixture import FixtureProvider
from sira.providers.tavily import TavilyProvider
from sira.storage import Cache


def response(results=None, **extra):
    payload = {
        "query": "example", "answer": None, "images": [],
        "results": results if results is not None else [
            {"title": "Example", "url": "https://example.org/a", "content": "Evidence.",
             "score": 0.9, "raw_content": None}
        ], "response_time": 0.1, "usage": {"credits": 1}, "request_id": "example-request",
    }
    payload.update(extra)
    return io.BytesIO(json.dumps(payload).encode())


class SiraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root)
        self.fixture = FixtureProvider(ROOT / "benchmarks/cases.json")

    def result(self, path):
        return json.loads((path / "result.json").read_text())

    def test_run_preserves_sources_citations_and_audit_identity(self):
        path = research("offline duplicate example", self.fixture, self.settings)
        result = self.result(path)
        self.assertEqual([s["url"] for s in result["sources"]],
                         ["https://example.org/a", "https://example.com/b"])
        self.assertEqual([c["source_id"] for c in result["citations"]], ["S1", "S2"])
        self.assertTrue(all(s["trust"] == "untrusted" for s in result["sources"]))
        events = [json.loads(line) for line in (path / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(events[0]["event"], "run_started")
        self.assertEqual(events[-1]["event"], "run_finished")
        self.assertTrue(all(e["run_id"] == result["run_id"] for e in events))
        self.assertEqual(len(result["code_sha256"]), 64)

    def test_unmeasured_quality_is_never_reported_as_success(self):
        result = self.result(research("offline duplicate example", self.fixture, self.settings))
        for metric in ("factual_accuracy", "citation_correctness", "citation_coverage",
                       "source_quality", "source_diversity"):
            self.assertIsNone(result["metrics"][metric])
        self.assertEqual(result["metrics"]["unique_hostnames"], 2)

    def test_prompt_injection_stays_inert_source_data(self):
        result = self.result(research("offline untrusted text example", self.fixture, self.settings))
        self.assertIn("touch SIRA_SHOULD_NOT_EXIST", result["sources"][0]["excerpt"])
        self.assertEqual(result["metrics"]["rejected_sources"], 1)
        self.assertFalse((ROOT / "SIRA_SHOULD_NOT_EXIST").exists())
        self.assertFalse((self.root / "SIRA_SHOULD_NOT_EXIST").exists())

    def test_empty_sources_are_not_retrieval_success(self):
        result = self.result(research("offline empty result example", self.fixture, self.settings))
        self.assertEqual(result["status"], "no_results")
        self.assertFalse(result["metrics"]["retrieval_success"])
        self.assertIsNone(result["metrics"]["citation_reference_resolution"])

    def test_cache_avoids_second_api_request_and_does_not_recharge_credits(self):
        with patch("sira.providers.tavily.open_request", return_value=response()) as http:
            provider = TavilyProvider("fake-key")
            first = self.result(research("example", provider, self.settings))
            second = self.result(research("example", provider, self.settings))
        self.assertEqual(http.call_count, 1)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertFalse(first["metrics"]["cache_hit"])
        self.assertTrue(second["metrics"]["cache_hit"])
        self.assertEqual(first["metrics"]["api_requests"], 1)
        self.assertEqual(second["metrics"]["api_requests"], 0)
        self.assertEqual(second["metrics"]["reported_credits"], 0)
        self.assertEqual(first["sources"], second["sources"])

    def test_cache_expiry_corruption_and_query_isolation(self):
        cache = Cache(self.root / "cache", ttl_seconds=60)
        with patch("sira.storage.time.time", return_value=100):
            cache.put("a", {"value": 3})
            self.assertEqual(cache.get("a"), {"value": 3})
            self.assertIsNone(cache.get("b"))
        with patch("sira.storage.time.time", return_value=161):
            self.assertIsNone(cache.get("a"))
        next((self.root / "cache").glob("*.json")).write_text("not json")
        self.assertIsNone(cache.get("a"))

    def test_settings_reject_unbounded_work(self):
        for value in (0, 6, -1):
            with self.assertRaises(ValueError):
                Settings(self.root, max_results=value)
        for query in (" ", "x" * 501):
            with self.assertRaises(ValueError):
                research(query, self.fixture, self.settings)

    def test_secret_loading_is_literal_and_environment_takes_precedence(self):
        env_file = self.root / ".env"
        env_file.write_text("TAVILY_API_KEY=local-key\n")
        with patch.dict(os.environ, {"TAVILY_API_KEY": "env-key"}):
            self.assertEqual(load_key(self.root), "env-key")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_key(self.root), "local-key")
            env_file.write_text("TAVILY_API_KEY=$(touch SHOULD_NOT_EXIST)\n")
            with self.assertRaises(ValueError):
                load_key(self.root)
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())

    def test_cli_missing_key_has_no_traceback_or_network(self):
        env = {k: v for k, v in os.environ.items() if k != "TAVILY_API_KEY"}
        proc = subprocess.run([sys.executable, str(ROOT / "sira.py"), "--root", str(self.root),
                               "research", "example"], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("TAVILY_API_KEY", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_benchmark_cli_records_actual_regression_outcome(self):
        proc = subprocess.run([sys.executable, str(ROOT / "sira.py"), "--root", str(self.root),
                               "benchmark"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(Path(json.loads(proc.stdout)["report"]).read_text())
        self.assertEqual(report["passed"], 3)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(len(report["dataset_sha256"]), 64)
        self.assertEqual(report["kind"], "synthetic_regression_not_live_quality")


class TavilyTests(unittest.TestCase):
    def test_truncated_http_response_is_saved_as_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("sira.providers.tavily.open_request", side_effect=IncompleteRead(b"partial")):
                path = research("example", TavilyProvider("fake-key"), Settings(Path(temp)))
            result = json.loads((path / "result.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["metrics"]["api_requests"], 1)
            self.assertEqual(json.loads((path / "audit.jsonl").read_text().splitlines()[-1])["event"],
                             "run_finished")

    def test_extreme_credit_usage_becomes_unknown_instead_of_crashing(self):
        with patch("sira.providers.tavily.open_request", return_value=response(usage={"credits": 10**400})):
            batch = TavilyProvider("fake-key").search("example", 3)
        self.assertIsNone(batch.credits)
        self.assertEqual(len(batch.sources), 1)

    def test_request_is_bounded_basic_and_key_only_in_header(self):
        with patch("sira.providers.tavily.open_request", return_value=response()) as http:
            batch = TavilyProvider("fake-key").search("example", 3)
        request = http.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.tavily.com/search")
        self.assertEqual(body["search_depth"], "basic")
        self.assertFalse(body["auto_parameters"])
        self.assertFalse(body["include_answer"])
        self.assertFalse(body["include_raw_content"])
        self.assertTrue(body["include_usage"])
        self.assertEqual(body["max_results"], 3)
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-key")
        self.assertNotIn(b"fake-key", request.data)
        self.assertEqual(batch.credits, 1)
        self.assertEqual(batch.sources[0].url, "https://example.org/a")

    def test_http_errors_are_redacted_and_not_retried(self):
        for status in (401, 429, 432, 433, 500, 302):
            with self.subTest(status=status), patch("sira.providers.tavily.open_request",
                 side_effect=HTTPError("https://api.tavily.com/search", status,
                                       "fake-key", {"Retry-After": "60"}, None)) as http:
                with self.assertRaises(ProviderError) as raised:
                    TavilyProvider("fake-key").search("example", 3)
                self.assertEqual(http.call_count, 1)
                self.assertEqual(raised.exception.code, f"http_{status}")
                self.assertNotIn("fake-key", str(raised.exception))

    def test_transport_errors_are_redacted(self):
        for error in (TimeoutError("fake-key"), URLError("fake-key")):
            with patch("sira.providers.tavily.open_request", side_effect=error):
                with self.assertRaises(ProviderError) as raised:
                    TavilyProvider("fake-key").search("example", 3)
                self.assertNotIn("fake-key", str(raised.exception))

    def test_malformed_and_oversized_responses_fail_explicitly(self):
        for data in (b"invalid", b"{}", b"[]", b'{"results":{}}', b"x" * (2 * 1024 * 1024 + 1)):
            with patch("sira.providers.tavily.open_request", return_value=io.BytesIO(data)):
                with self.assertRaises(ProviderError):
                    TavilyProvider("fake-key").search("example", 3)

    def test_invalid_urls_and_non_string_excerpts_are_rejected(self):
        rows = [{"title": "Bad", "url": url, "content": "text"} for url in
                ("file:///etc/passwd", "https://user:password@example.org", "https://", "https://exa mple.org")]
        rows.append({"title": "Bad", "url": "https://example.org", "content": {"command": "run"}})
        with patch("sira.providers.tavily.open_request", return_value=response(results=rows)):
            batch = TavilyProvider("fake-key").search("example", 5)
        self.assertEqual(len(batch.sources), 0)
        self.assertEqual(batch.rejected_sources, 5)

    def test_failure_run_records_request_and_unknown_credits_without_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("sira.providers.tavily.open_request", side_effect=TimeoutError("fake-key")):
                path = research("example", TavilyProvider("fake-key"), Settings(Path(temp)))
            data = json.loads((path / "result.json").read_text())
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["metrics"]["api_requests"], 1)
            self.assertIsNone(data["metrics"]["reported_credits"])
            self.assertEqual(data["metrics"]["failures"], 1)
            for file in path.iterdir():
                self.assertNotIn("fake-key", file.read_text())


if __name__ == "__main__":
    unittest.main()
