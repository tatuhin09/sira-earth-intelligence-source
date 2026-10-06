"""Semantic Scholar paper search tests; all provider tests mock network."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.cli import main
from sira.config import Settings, load_optional_key
from sira.models import ProviderError
from sira.papers import Paper, PaperBatch, papers_run
from sira.providers.semantic_scholar import SemanticScholarProvider


def api_response(rows=None, **extra):
    payload = {
        "total": 2,
        "offset": 0,
        "next": 2,
        "data": rows if rows is not None else [
            {
                "paperId": "paper-1",
                "title": "Grounded Research Systems",
                "abstract": "A bounded evidence workflow.",
                "authors": [{"authorId": "a1", "name": "Ada Researcher"}],
                "year": 2026,
                "url": "https://www.semanticscholar.org/paper/paper-1",
                "externalIds": {"DOI": "10.1000/example.1"},
                "openAccessPdf": {"url": "https://example.org/paper-1.pdf", "status": "GREEN"},
            },
            {
                "paperId": "paper-2",
                "title": "Evidence Verification",
                "abstract": None,
                "authors": [{"authorId": "a2", "name": "Ben Scholar"}],
                "year": 2025,
                "url": "https://www.semanticscholar.org/paper/paper-2",
                "externalIds": {},
                "openAccessPdf": None,
            },
        ],
    }
    payload.update(extra)
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class FakePaperProvider:
    name = "paper_fixture"
    cache_namespace = "paper-fixture-v1"

    def __init__(self, papers):
        self.papers = tuple(papers)
        self.calls = 0

    def search(self, query, max_results):
        self.calls += 1
        return PaperBatch(self.papers[:max_results], api_requests=0, rejected_papers=0)


class PaperRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root, max_results=3, cache_ttl_seconds=86400)

    def paper(self, paper_id="p1", doi="10.1000/x", url="https://example.org/p1"):
        return Paper(
            paper_id=paper_id,
            title="A paper",
            abstract="Abstract text",
            authors=("Ada", "Ben"),
            year=2026,
            doi=doi,
            url=url,
            open_access_pdf_url="https://example.org/p1.pdf",
            retrieved_at="2026-09-17T00:00:00+00:00",
        )

    def load(self, path):
        return json.loads((path / "papers.json").read_text(encoding="utf-8"))

    def test_run_persists_metadata_deduplicates_and_audits(self):
        duplicate = self.paper("p2", doi="10.1000/x", url="https://example.org/p2")
        provider = FakePaperProvider((self.paper(), duplicate))
        path = papers_run(self.root, " evidence   verification ", provider, self.settings, use_cache=False)
        data = self.load(path)
        self.assertEqual(data["question"], "evidence verification")
        self.assertEqual(data["status"], "completed")
        self.assertEqual(len(data["papers"]), 1)
        self.assertEqual(data["papers"][0]["authors"], ["Ada", "Ben"])
        self.assertEqual(data["papers"][0]["doi"], "10.1000/x")
        self.assertEqual(data["metrics"]["duplicates_removed"], 1)
        self.assertEqual(data["metrics"]["paper_count"], 1)
        events = [json.loads(line)["event"] for line in
                  (path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events, ["run_started", "provider_started", "provider_finished", "run_finished"])

    def test_cache_avoids_second_provider_call_and_creates_new_run(self):
        provider = FakePaperProvider((self.paper(),))
        first_path = papers_run(self.root, "example", provider, self.settings)
        second_path = papers_run(self.root, "example", provider, self.settings)
        first, second = self.load(first_path), self.load(second_path)
        self.assertEqual(provider.calls, 1)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertFalse(first["metrics"]["cache_hit"])
        self.assertTrue(second["metrics"]["cache_hit"])
        self.assertEqual(second["metrics"]["api_requests"], 0)
        self.assertEqual(first["papers"], second["papers"])

    def test_empty_query_and_over_three_results_are_rejected(self):
        provider = FakePaperProvider((self.paper(),))
        for query in (" ", "x" * 501):
            with self.subTest(query_length=len(query)), self.assertRaises(ValueError):
                papers_run(self.root, query, provider, self.settings)
        with self.assertRaises(ValueError):
            papers_run(self.root, "ok", provider, Settings(self.root, max_results=4))

    def test_provider_failure_is_safe_and_records_actual_attempt_count(self):
        class Failing:
            name = "broken"
            cache_namespace = "broken-v1"
            def search(self, query, max_results):
                raise ProviderError("rate_limited", True, 9, request_count=3)
        path = papers_run(self.root, "example", Failing(), self.settings, use_cache=False)
        data = self.load(path)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"], {"code": "rate_limited", "retry_after_seconds": 9})
        self.assertEqual(data["metrics"]["api_requests"], 3)
        self.assertEqual(data["metrics"]["failures"], 1)


class SemanticScholarProviderTests(unittest.TestCase):
    def test_request_is_one_anonymous_bounded_get_and_metadata_is_parsed(self):
        with patch("sira.providers.semantic_scholar.open_request", return_value=api_response()) as http:
            batch = SemanticScholarProvider(timeout=7).search("self-supervised learning", 3)
        self.assertEqual(http.call_count, 1)
        request = http.call_args.args[0]
        timeout = http.call_args.kwargs["timeout"]
        parsed = urlsplit(request.full_url)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "api.semanticscholar.org")
        self.assertEqual(parsed.path, "/graph/v1/paper/search")
        self.assertEqual(params["query"], ["self supervised learning"])
        self.assertEqual(params["limit"], ["3"])
        self.assertEqual(params["fields"], ["title,abstract,authors,year,url,externalIds,openAccessPdf"])
        self.assertIsNone(request.data)
        self.assertIsNone(request.get_header("X-api-key"))
        self.assertEqual(timeout, 7)
        self.assertEqual(batch.api_requests, 1)
        self.assertEqual(batch.rejected_papers, 0)
        self.assertEqual(len(batch.papers), 2)
        paper = batch.papers[0]
        self.assertEqual(paper.authors, ("Ada Researcher",))
        self.assertEqual(paper.year, 2026)
        self.assertEqual(paper.doi, "10.1000/example.1")
        self.assertEqual(paper.open_access_pdf_url, "https://example.org/paper-1.pdf")

    def test_invalid_required_rows_are_rejected_and_bad_optional_pdf_is_omitted(self):
        rows = [
            {"paperId": "ok", "title": "Valid", "abstract": "Text", "authors": [], "year": 2024,
             "url": "https://www.semanticscholar.org/paper/ok", "externalIds": {},
             "openAccessPdf": {"url": "file:///etc/passwd"}},
            {"paperId": "bad-url", "title": "Bad", "abstract": None, "authors": [], "year": 2024,
             "url": "file:///etc/passwd", "externalIds": {}, "openAccessPdf": None},
            {"paperId": "bad-abstract", "title": "Bad", "abstract": {"x": 1}, "authors": [], "year": 2024,
             "url": "https://example.org/y", "externalIds": {}, "openAccessPdf": None},
        ]
        with patch("sira.providers.semantic_scholar.open_request", return_value=api_response(rows)):
            batch = SemanticScholarProvider().search("example", 3)
        self.assertEqual(len(batch.papers), 1)
        self.assertIsNone(batch.papers[0].open_access_pdf_url)
        self.assertEqual(batch.rejected_papers, 2)

    def test_malformed_optional_metadata_is_omitted_without_dropping_paper(self):
        rows = [{
            "paperId": "optional-bad", "title": "Still valid", "abstract": None,
            "authors": [{"name": "Valid Author"}, {"name": ""}, {"bad": "shape"}],
            "year": 10000, "url": "https://www.semanticscholar.org/paper/optional-bad",
            "externalIds": {"DOI": ""},
            "openAccessPdf": {"url": "file:///private.pdf"},
        }]
        with patch("sira.providers.semantic_scholar.open_request", return_value=api_response(rows)):
            batch = SemanticScholarProvider().search("example", 3)
        self.assertEqual(batch.rejected_papers, 0)
        self.assertEqual(len(batch.papers), 1)
        paper = batch.papers[0]
        self.assertEqual(paper.authors, ("Valid Author",))
        self.assertIsNone(paper.year)
        self.assertIsNone(paper.doi)
        self.assertIsNone(paper.open_access_pdf_url)

    def test_invalid_limit_and_bad_response_fail_without_retry(self):
        provider = SemanticScholarProvider()
        for limit in (0, 4):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                provider.search("example", limit)
        with patch("sira.providers.semantic_scholar.open_request", return_value=io.BytesIO(b"{}")) as http:
            with self.assertRaises(ProviderError) as raised:
                provider.search("example", 3)
        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertEqual(http.call_count, 1)

    def test_http_429_retries_with_retry_after_then_succeeds(self):
        errors = [
            HTTPError("https://api.semanticscholar.org/graph/v1/paper/search", 429,
                      "Too many requests", {"Retry-After": "1"}, None),
            HTTPError("https://api.semanticscholar.org/graph/v1/paper/search", 429,
                      "Too many requests", {"Retry-After": "1"}, None),
        ]
        with patch("sira.providers.semantic_scholar.open_request",
                   side_effect=[*errors, api_response()]) as http, \
             patch("sira.providers.semantic_scholar.sleep") as sleeper:
            batch = SemanticScholarProvider().search("example", 3)
        self.assertEqual(http.call_count, 3)
        self.assertEqual(sleeper.call_count, 2)
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [1.0, 1.0])
        self.assertEqual(batch.api_requests, 3)
        self.assertEqual(len(batch.papers), 2)

    def test_http_429_without_retry_after_uses_bounded_backoff_then_reports_rate_limit(self):
        errors = [
            HTTPError("https://api.semanticscholar.org/graph/v1/paper/search", 429,
                      "Too many requests", {}, None)
            for _ in range(3)
        ]
        with patch("sira.providers.semantic_scholar.open_request", side_effect=errors) as http, \
             patch("sira.providers.semantic_scholar.sleep") as sleeper:
            with self.assertRaises(ProviderError) as raised:
                SemanticScholarProvider().search("example", 3)
        self.assertEqual(http.call_count, 3)
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [0.5, 1.0])
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(raised.exception.request_count, 3)
        self.assertIsNone(raised.exception.retry_after)

    def test_long_retry_after_is_not_slept_and_is_reported_immediately(self):
        error = HTTPError("https://api.semanticscholar.org/graph/v1/paper/search", 429,
                          "Too many requests", {"Retry-After": "60"}, None)
        with patch("sira.providers.semantic_scholar.open_request", side_effect=error) as http, \
             patch("sira.providers.semantic_scholar.sleep") as sleeper:
            with self.assertRaises(ProviderError) as raised:
                SemanticScholarProvider().search("example", 3)
        self.assertEqual(http.call_count, 1)
        sleeper.assert_not_called()
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(raised.exception.request_count, 1)
        self.assertEqual(raised.exception.retry_after, 60)

    def test_optional_api_key_is_sent_only_when_configured(self):
        with patch("sira.providers.semantic_scholar.open_request", return_value=api_response()) as http:
            SemanticScholarProvider(api_key="paper-key").search("example", 3)
        request = http.call_args.args[0]
        self.assertEqual(request.get_header("X-api-key"), "paper-key")


class SemanticScholarCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_optional_key_absent_returns_none_and_env_takes_precedence(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(load_optional_key(self.root, "SIRA_SEMANTIC_SCHOLAR_API_KEY"))
            (self.root / ".env").write_text(
                "SIRA_SEMANTIC_SCHOLAR_API_KEY=local-paper-key\n", encoding="utf-8")
            self.assertEqual(load_optional_key(self.root, "SIRA_SEMANTIC_SCHOLAR_API_KEY"),
                             "local-paper-key")
            with patch.dict(os.environ, {"SIRA_SEMANTIC_SCHOLAR_API_KEY": "env-paper-key"}):
                self.assertEqual(load_optional_key(self.root, "SIRA_SEMANTIC_SCHOLAR_API_KEY"),
                                 "env-paper-key")

    def test_setup_paper_key_saves_hidden_optional_key_once(self):
        with patch("sira.cli.getpass.getpass", return_value="paper-local"):
            self.assertEqual(main(["--root", str(self.root), "setup-paper-key"]), 0)
        self.assertEqual(load_optional_key(self.root, "SIRA_SEMANTIC_SCHOLAR_API_KEY"),
                         "paper-local")
        with patch("sira.cli.getpass.getpass", return_value="replacement"):
            self.assertEqual(main(["--root", str(self.root), "setup-paper-key"]), 2)
        self.assertNotIn("replacement", (self.root / ".env").read_text(encoding="utf-8"))


class PaperCliBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_offline_paper_benchmark_cli_has_four_cases_and_zero_api_requests(self):
        proc = subprocess.run([sys.executable, str(ROOT / "sira.py"), "--root", str(self.root),
                               "benchmark", "--papers"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        report = json.loads(Path(summary["report"]).read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(report["passed"], 4)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(report["kind"], "synthetic_paper_provider_regression_not_live_quality")
        self.assertEqual(len(report["dataset_sha256"]), 64)

    def test_papers_cli_rejects_oversized_question_before_network(self):
        proc = subprocess.run([sys.executable, str(ROOT / "sira.py"), "--root", str(self.root),
                               "papers", "x" * 501], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("1..500", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
