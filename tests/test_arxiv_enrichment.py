"""arXiv metadata and cross-provider enrichment tests; no live network."""
import io
import json
import importlib
from contextlib import redirect_stdout
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


def arxiv_response(entries: str) -> io.BytesIO:
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
      <title>arXiv Query: test</title>
      {entries}
    </feed>'''
    return io.BytesIO(xml.encode("utf-8"))


def sample_entry(*, arxiv_id="2609.12345v2", title="Evidence Verification for Artificial Intelligence",
                 doi="10.1000/arxiv.1", summary="A grounded verification method for AI systems.") -> str:
    doi_xml = f"<arxiv:doi>{doi}</arxiv:doi>" if doi is not None else ""
    return f'''<entry>
      <id>http://arxiv.org/abs/{arxiv_id}</id>
      <updated>2026-09-17T10:00:00Z</updated>
      <published>2026-09-16T09:30:00Z</published>
      <title>  {title} </title>
      <summary>  {summary} </summary>
      <author><name>Ada Researcher</name></author>
      <author><name>Ben Scholar</name></author>
      {doi_xml}
      <link href="http://arxiv.org/abs/{arxiv_id}" rel="alternate" type="text/html"/>
      <link title="pdf" href="http://arxiv.org/pdf/{arxiv_id}" rel="related" type="application/pdf"/>
    </entry>'''


def make_paper(*, provider="crossref", paper_id="crossref:10.1000/shared",
               title="Evidence Verification for Artificial Intelligence", abstract=None,
               doi="10.1000/shared", pdf=None, published_date=None, providers=()):
    return Paper(
        paper_id=paper_id,
        title=title,
        abstract=abstract,
        authors=("Ada Researcher",),
        year=2026,
        doi=doi,
        url=("https://doi.org/" + doi) if doi else "https://example.org/paper/" + paper_id.replace(":", "-"),
        open_access_pdf_url=pdf,
        retrieved_at="2026-09-17T00:00:00+00:00",
        provider=provider,
        published_date=published_date,
        providers=providers,
    )


class FakeProvider:
    def __init__(self, name, *, papers=(), error=None, terminal_error=None, api_requests=1):
        self.name = name
        self.cache_namespace = name + "-v-test"
        self._papers = tuple(papers)
        self._error = error
        self._terminal_error = terminal_error
        self._api_requests = api_requests
        self.calls = 0

    def search(self, query, max_results):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return PaperBatch(
            self._papers[:max_results],
            api_requests=self._api_requests,
            providers_attempted=(self.name,),
            terminal_error=self._terminal_error,
        )


class ArxivBootstrapTests(unittest.TestCase):
    def test_arxiv_provider_class_exists(self):
        module = importlib.import_module("sira.providers.arxiv")
        self.assertTrue(hasattr(module, "ArxivProvider"))


class RouterBootstrapTests(unittest.TestCase):
    def test_enriching_provider_class_exists(self):
        module = importlib.import_module("sira.providers.paper_router")
        self.assertTrue(hasattr(module, "EnrichingPaperProvider"))


class ArxivProviderTests(unittest.TestCase):
    def test_request_is_bounded_get_and_atom_metadata_is_normalized(self):
        from sira.providers.arxiv import ArxivProvider

        with patch("sira.providers.arxiv.open_request", return_value=arxiv_response(sample_entry())) as http:
            batch = ArxivProvider(timeout=7).search("evidence verification", 3)

        self.assertEqual(http.call_count, 1)
        request = http.call_args.args[0]
        self.assertEqual(http.call_args.kwargs["timeout"], 7)
        parsed = urlsplit(request.full_url)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "export.arxiv.org")
        self.assertEqual(parsed.path, "/api/query")
        self.assertEqual(params["search_query"], ["all:evidence AND all:verification"])
        self.assertEqual(params["start"], ["0"])
        self.assertEqual(params["max_results"], ["3"])
        self.assertEqual(params["sortBy"], ["relevance"])
        self.assertEqual(params["sortOrder"], ["descending"])
        self.assertIsNone(request.data)
        self.assertEqual(batch.api_requests, 1)
        self.assertEqual(batch.rejected_papers, 0)
        self.assertEqual(batch.providers_attempted, ("arxiv",))
        self.assertEqual(len(batch.papers), 1)
        paper = batch.papers[0]
        self.assertEqual(paper.paper_id, "arxiv:2609.12345")
        self.assertEqual(paper.title, "Evidence Verification for Artificial Intelligence")
        self.assertEqual(paper.abstract, "A grounded verification method for AI systems.")
        self.assertEqual(paper.authors, ("Ada Researcher", "Ben Scholar"))
        self.assertEqual(paper.year, 2026)
        self.assertEqual(paper.published_date, "2026-09-16")
        self.assertEqual(paper.doi, "10.1000/arxiv.1")
        self.assertEqual(paper.url, "https://arxiv.org/abs/2609.12345")
        self.assertEqual(paper.open_access_pdf_url, "https://arxiv.org/pdf/2609.12345")
        self.assertEqual(paper.provider, "arxiv")
        self.assertEqual(paper.providers, ("arxiv",))

    def test_bad_entries_are_rejected_without_poisoning_valid_entries(self):
        from sira.providers.arxiv import ArxivProvider

        entries = sample_entry() + '''<entry>
          <id>file:///etc/passwd</id><published>2026-09-16T09:30:00Z</published>
          <title>Bad id</title><summary>bad</summary>
        </entry>'''
        with patch("sira.providers.arxiv.open_request", return_value=arxiv_response(entries)):
            batch = ArxivProvider().search("example", 3)
        self.assertEqual(len(batch.papers), 1)
        self.assertEqual(batch.rejected_papers, 1)

    def test_http_error_and_hostile_xml_are_safe_provider_errors(self):
        from sira.providers.arxiv import ArxivProvider

        error = HTTPError("https://export.arxiv.org/api/query", 429, "Too many", {}, None)
        with patch("sira.providers.arxiv.open_request", side_effect=error):
            with self.assertRaises(ProviderError) as raised:
                ArxivProvider().search("example", 3)
        self.assertEqual(raised.exception.code, "http_429")
        self.assertEqual(raised.exception.request_count, 1)

        hostile = io.BytesIO(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><feed/>')
        with patch("sira.providers.arxiv.open_request", return_value=hostile):
            with self.assertRaises(ProviderError) as raised:
                ArxivProvider().search("example", 3)
        self.assertEqual(raised.exception.code, "invalid_response")


class EnrichingPaperProviderTests(unittest.TestCase):
    def test_duplicate_doi_is_enriched_without_replacing_primary_identity(self):
        from sira.providers.paper_router import EnrichingPaperProvider

        base_paper = make_paper()
        arxiv_paper = make_paper(
            provider="arxiv", paper_id="arxiv:2609.12345", abstract="ArXiv abstract",
            pdf="https://arxiv.org/pdf/2609.12345", published_date="2026-09-16",
            providers=("arxiv",),
        )
        base = FakeProvider("paper_auto", papers=(base_paper,), api_requests=2)
        arxiv = FakeProvider("arxiv", papers=(arxiv_paper,), api_requests=1)
        batch = EnrichingPaperProvider(base, arxiv).search("evidence verification artificial intelligence", 3)

        self.assertEqual(base.calls, 1)
        self.assertEqual(arxiv.calls, 1)
        self.assertEqual(batch.api_requests, 3)
        self.assertTrue(batch.enrichment_used)
        self.assertEqual(batch.papers_enriched, 1)
        self.assertEqual(batch.papers_merged, 1)
        self.assertEqual(batch.providers_attempted, ("paper_auto", "arxiv"))
        self.assertEqual(len(batch.papers), 1)
        merged = batch.papers[0]
        self.assertEqual(merged.paper_id, base_paper.paper_id)
        self.assertEqual(merged.provider, "crossref")
        self.assertEqual(merged.providers, ("crossref", "arxiv"))
        self.assertEqual(merged.abstract, "ArXiv abstract")
        self.assertEqual(merged.open_access_pdf_url, "https://arxiv.org/pdf/2609.12345")
        self.assertEqual(merged.published_date, "2026-09-16")

    def test_three_rich_base_results_skip_unnecessary_arxiv_request(self):
        from sira.providers.paper_router import EnrichingPaperProvider

        rich = tuple(make_paper(
            paper_id=f"crossref:10.1000/{i}", doi=f"10.1000/{i}", abstract="Complete abstract",
            pdf=f"https://example.org/{i}.pdf") for i in range(3))
        base = FakeProvider("paper_auto", papers=rich, api_requests=1)
        arxiv = FakeProvider("arxiv", papers=(make_paper(provider="arxiv"),), api_requests=1)
        batch = EnrichingPaperProvider(base, arxiv).search("example", 3)

        self.assertEqual(arxiv.calls, 0)
        self.assertFalse(batch.enrichment_used)
        self.assertEqual(batch.api_requests, 1)
        self.assertEqual(len(batch.papers), 3)

    def test_arxiv_recovers_when_base_chain_has_terminal_failure(self):
        from sira.providers.paper_router import EnrichingPaperProvider

        base = FakeProvider("paper_auto", terminal_error="all_paper_providers_failed", api_requests=2)
        arxiv_paper = make_paper(provider="arxiv", paper_id="arxiv:2609.9", doi=None,
                                 abstract="Recovered", pdf="https://arxiv.org/pdf/2609.9")
        arxiv = FakeProvider("arxiv", papers=(arxiv_paper,), api_requests=1)
        batch = EnrichingPaperProvider(base, arxiv).search("example", 3)

        self.assertIsNone(batch.terminal_error)
        self.assertEqual(len(batch.papers), 1)
        self.assertEqual(batch.api_requests, 3)
        self.assertEqual(batch.providers_attempted, ("paper_auto", "arxiv"))
        self.assertTrue(batch.fallback_used)

    def test_run_persists_enrichment_metrics_and_provider_provenance(self):
        from sira.providers.paper_router import EnrichingPaperProvider

        base = FakeProvider("paper_auto", papers=(make_paper(),), api_requests=1)
        arxiv = FakeProvider("arxiv", papers=(make_paper(
            provider="arxiv", paper_id="arxiv:2609.12345", abstract="ArXiv abstract",
            pdf="https://arxiv.org/pdf/2609.12345", published_date="2026-09-16"),), api_requests=1)
        provider = EnrichingPaperProvider(base, arxiv)
        with tempfile.TemporaryDirectory() as temp:
            path = papers_run(Path(temp), "evidence verification", provider,
                              Settings(Path(temp), max_results=3), use_cache=False)
            data = json.loads((path / "papers.json").read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "completed")
        self.assertTrue(data["metrics"]["enrichment_used"])
        self.assertEqual(data["metrics"]["papers_enriched"], 1)
        self.assertEqual(data["metrics"]["papers_merged"], 1)
        self.assertEqual(data["papers"][0]["providers"], ["crossref", "arxiv"])
        self.assertEqual(data["papers"][0]["published_date"], "2026-09-16")


class ArxivCliTests(unittest.TestCase):
    def test_provider_flag_can_force_arxiv(self):
        paper = make_paper(provider="arxiv", paper_id="arxiv:2609.1", doi=None,
                           abstract="Abstract", pdf="https://arxiv.org/pdf/2609.1")
        with tempfile.TemporaryDirectory() as temp, \
             patch("sira.cli.ArxivProvider.search", return_value=PaperBatch(
                 (paper,), api_requests=1, providers_attempted=("arxiv",))) as arxiv:
            output = io.StringIO()
            with redirect_stdout(output):
                rc = main(["--root", temp, "papers", "evidence verification", "--provider", "arxiv",
                           "--no-cache"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "completed")
        arxiv.assert_called_once()


if __name__ == "__main__":
    unittest.main()
