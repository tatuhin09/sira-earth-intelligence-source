"""Crossref and multi-provider scholarly-paper fallback tests; network is always mocked."""
import io
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.cli import main
from sira.config import Settings
from sira.models import ProviderError
from sira.papers import Paper, PaperBatch, papers_run
from sira.providers.crossref import CrossrefProvider
from sira.providers.paper_router import FallbackPaperProvider


def crossref_response(items=None):
    payload = {
        "status": "ok",
        "message-type": "work-list",
        "message-version": "1.0.0",
        "message": {
            "items": items if items is not None else [
                {
                    "DOI": "10.1000/example.1",
                    "title": ["Grounded Research Systems"],
                    "abstract": "<jats:p>A <jats:bold>bounded</jats:bold> evidence workflow.</jats:p>",
                    "author": [{"given": "Ada", "family": "Researcher"}],
                    "published-print": {"date-parts": [[2026, 3, 1]]},
                    "URL": "https://doi.org/10.1000/example.1",
                    "link": [{
                        "URL": "https://example.org/paper-1.pdf",
                        "content-type": "application/pdf",
                    }],
                },
                {
                    "DOI": "10.1000/example.2",
                    "title": ["Evidence Verification"],
                    "author": [{"name": "Research Consortium"}],
                    "published-online": {"date-parts": [[2025]]},
                    "URL": "https://doi.org/10.1000/example.2",
                },
            ]
        },
    }
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class CrossrefProviderTests(unittest.TestCase):
    def test_request_is_bounded_public_get_and_metadata_is_normalized(self):
        with patch("sira.providers.crossref.open_request", return_value=crossref_response()) as http:
            batch = CrossrefProvider(timeout=7).search("evidence verification", 3)
        self.assertEqual(http.call_count, 1)
        request = http.call_args.args[0]
        self.assertEqual(http.call_args.kwargs["timeout"], 7)
        parsed = urlsplit(request.full_url)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "api.crossref.org")
        self.assertEqual(parsed.path, "/works")
        self.assertEqual(params["query.bibliographic"], ["evidence verification"])
        self.assertEqual(params["rows"], ["3"])
        self.assertIn("DOI,title,author", params["select"][0])
        self.assertIsNone(request.data)
        self.assertEqual(batch.api_requests, 1)
        self.assertEqual(batch.rejected_papers, 0)
        self.assertEqual(len(batch.papers), 2)
        paper = batch.papers[0]
        self.assertEqual(paper.provider, "crossref")
        self.assertEqual(paper.paper_id, "crossref:10.1000/example.1")
        self.assertEqual(paper.title, "Grounded Research Systems")
        self.assertEqual(paper.abstract, "A bounded evidence workflow.")
        self.assertEqual(paper.authors, ("Ada Researcher",))
        self.assertEqual(paper.year, 2026)
        self.assertEqual(paper.doi, "10.1000/example.1")
        self.assertEqual(paper.url, "https://doi.org/10.1000/example.1")
        self.assertIsNone(paper.open_access_pdf_url)

    def test_bad_required_rows_are_rejected_and_optional_metadata_is_safely_omitted(self):
        items = [
            {
                "DOI": "10.1000/valid",
                "title": ["Valid paper"],
                "author": [{"given": "Ada", "family": "Lovelace"}, {"family": "Solo"}],
                "published": {"date-parts": [[10000]]},
                "URL": "https://doi.org/10.1000/valid",
                "abstract": {"bad": "shape"},
                "link": [{"URL": "file:///etc/passwd", "content-type": "application/pdf"}],
            },
            {"DOI": "", "title": ["No DOI"], "URL": "https://example.org/no-doi"},
            {"DOI": "10.1000/no-title", "title": [], "URL": "https://doi.org/10.1000/no-title"},
            {"DOI": "10.1000/bad-url", "title": ["Bad URL"], "URL": "file:///etc/passwd"},
        ]
        with patch("sira.providers.crossref.open_request", return_value=crossref_response(items)):
            batch = CrossrefProvider().search("example", 3)
        self.assertEqual(len(batch.papers), 1)
        self.assertEqual(batch.rejected_papers, 2)  # rows are bounded to max_results=3
        paper = batch.papers[0]
        self.assertEqual(paper.authors, ("Ada Lovelace", "Solo"))
        self.assertIsNone(paper.abstract)
        self.assertIsNone(paper.year)
        self.assertIsNone(paper.open_access_pdf_url)

    def test_http_error_is_redacted_and_request_count_is_preserved(self):
        error = HTTPError("https://api.crossref.org/works", 429, "Too many", {}, None)
        with patch("sira.providers.crossref.open_request", side_effect=error):
            with self.assertRaises(ProviderError) as raised:
                CrossrefProvider().search("example", 3)
        self.assertEqual(raised.exception.code, "http_429")
        self.assertEqual(raised.exception.request_count, 1)


class FakeProvider:
    def __init__(self, name, *, papers=(), error=None, api_requests=1):
        self.name = name
        self.cache_namespace = name + "-v1"
        self._papers = tuple(papers)
        self._error = error
        self._api_requests = api_requests
        self.calls = 0

    def search(self, query, max_results):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return PaperBatch(self._papers[:max_results], api_requests=self._api_requests)


def paper(provider="crossref", paper_id="p1", doi="10.1000/x"):
    return Paper(
        paper_id=paper_id,
        title="A paper",
        abstract="Abstract",
        authors=("Ada",),
        year=2026,
        doi=doi,
        url="https://doi.org/" + doi,
        open_access_pdf_url=None,
        retrieved_at="2026-09-17T00:00:00+00:00",
        provider=provider,
    )


class FallbackPaperProviderTests(unittest.TestCase):
    def test_primary_success_does_not_call_fallback(self):
        primary = FakeProvider("semantic_scholar", papers=(paper("semantic_scholar"),))
        fallback = FakeProvider("crossref", papers=(paper("crossref", "p2", "10.1000/y"),))
        batch = FallbackPaperProvider((primary, fallback)).search("example", 3)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(batch.providers_attempted, ("semantic_scholar",))
        self.assertFalse(batch.fallback_used)
        self.assertEqual(batch.api_requests, 1)
        self.assertEqual(batch.papers[0].provider, "semantic_scholar")

    def test_rate_limited_primary_falls_back_and_aggregates_requests(self):
        primary = FakeProvider(
            "semantic_scholar",
            error=ProviderError("rate_limited", True, request_count=1),
        )
        fallback = FakeProvider("crossref", papers=(paper(),), api_requests=1)
        batch = FallbackPaperProvider((primary, fallback)).search("example", 3)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(batch.api_requests, 2)
        self.assertEqual(batch.providers_attempted, ("semantic_scholar", "crossref"))
        self.assertTrue(batch.fallback_used)
        self.assertEqual(batch.provider_errors, (("semantic_scholar", "rate_limited"),))
        self.assertEqual(len(batch.papers), 1)

    def test_empty_primary_also_falls_back(self):
        primary = FakeProvider("semantic_scholar", papers=())
        fallback = FakeProvider("crossref", papers=(paper(),))
        batch = FallbackPaperProvider((primary, fallback)).search("example", 3)
        self.assertTrue(batch.fallback_used)
        self.assertEqual(batch.providers_attempted, ("semantic_scholar", "crossref"))
        self.assertEqual(len(batch.papers), 1)

    def test_all_failures_return_one_safe_aggregate_batch_with_total_request_count(self):
        primary = FakeProvider(
            "semantic_scholar", error=ProviderError("rate_limited", True, request_count=1))
        fallback = FakeProvider(
            "crossref", error=ProviderError("network_or_timeout", True, request_count=1))
        batch = FallbackPaperProvider((primary, fallback)).search("example", 3)
        self.assertEqual(batch.terminal_error, "all_paper_providers_failed")
        self.assertEqual(batch.api_requests, 2)
        self.assertEqual(batch.providers_attempted, ("semantic_scholar", "crossref"))
        self.assertEqual(batch.provider_errors, (("semantic_scholar", "rate_limited"),
                                                 ("crossref", "network_or_timeout")))

    def test_all_failed_run_preserves_provider_diagnostics_without_raw_http_content(self):
        primary = FakeProvider(
            "semantic_scholar", error=ProviderError("rate_limited", True, request_count=1))
        fallback = FakeProvider(
            "crossref", error=ProviderError("network_or_timeout", True, request_count=1))
        provider = FallbackPaperProvider((primary, fallback))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = papers_run(root, "example", provider, Settings(root, max_results=3), use_cache=False)
            data = json.loads((path / "papers.json").read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"], {"code": "all_paper_providers_failed",
                                         "retry_after_seconds": None})
        self.assertEqual(data["metrics"]["api_requests"], 2)
        self.assertEqual(data["metrics"]["providers_attempted"],
                         ["semantic_scholar", "crossref"])
        self.assertEqual(data["metrics"]["provider_failures"], 2)

    def test_run_records_fallback_observability(self):
        primary = FakeProvider(
            "semantic_scholar", error=ProviderError("rate_limited", True, request_count=1))
        fallback = FakeProvider("crossref", papers=(paper(),), api_requests=1)
        provider = FallbackPaperProvider((primary, fallback))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = papers_run(root, "example", provider, Settings(root, max_results=3), use_cache=False)
            data = json.loads((path / "papers.json").read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["metrics"]["api_requests"], 2)
        self.assertEqual(data["metrics"]["providers_attempted"], ["semantic_scholar", "crossref"])
        self.assertTrue(data["metrics"]["fallback_used"])
        self.assertEqual(data["metrics"]["provider_failures"], 1)
        self.assertEqual(data["papers"][0]["provider"], "crossref")


class PaperCliFallbackTests(unittest.TestCase):
    def test_default_auto_provider_falls_back_to_crossref_without_user_action(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch("sira.cli.SemanticScholarProvider.search",
                   side_effect=ProviderError("rate_limited", True, request_count=1)), \
             patch("sira.cli.CrossrefProvider.search",
                   return_value=PaperBatch((paper(),), api_requests=1,
                                           providers_attempted=("crossref",))) as crossref, \
             patch("sira.cli.ArxivProvider.search",
                   return_value=PaperBatch((), api_requests=1,
                                           providers_attempted=("arxiv",))) as arxiv:
            output = io.StringIO()
            with redirect_stdout(output):
                rc = main(["--root", temp, "papers", "evidence verification", "--no-cache"])
        self.assertEqual(rc, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["metrics"]["fallback_used"])
        self.assertTrue(result["metrics"]["enrichment_used"])
        self.assertEqual(result["metrics"]["providers_attempted"],
                         ["semantic_scholar", "crossref", "arxiv"])
        self.assertEqual(result["metrics"]["api_requests"], 3)
        crossref.assert_called_once()
        arxiv.assert_called_once()

    def test_provider_flag_can_force_crossref_for_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch("sira.cli.CrossrefProvider.search",
                   return_value=PaperBatch((paper(),), api_requests=1,
                                           providers_attempted=("crossref",))) as crossref, \
             patch("sira.cli.SemanticScholarProvider.search") as semantic:
            output = io.StringIO()
            with redirect_stdout(output):
                rc = main(["--root", temp, "papers", "example", "--provider", "crossref",
                           "--no-cache"])
        self.assertEqual(rc, 0)
        semantic.assert_not_called()
        crossref.assert_called_once()


if __name__ == "__main__":
    unittest.main()
