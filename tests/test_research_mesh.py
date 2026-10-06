from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [item for item in sys.path if item != str(SRC)]

from sira.papers import Paper, PaperBatch
from sira.provider_catalog import get_provider
from sira.research_mesh import research_mesh_plan, search_biomedical_free


class FakeProvider:
    def __init__(self, name, papers):
        self.name = name
        self.cache_namespace = "test-" + name
        self.papers = papers
        self.calls = 0

    def search(self, query, max_results):
        self.calls += 1
        return PaperBatch(
            tuple(self.papers[:max_results]),
            api_requests=0,
            providers_attempted=(self.name,),
        )


def make_paper(provider, pid, title, doi, abstract=None):
    return Paper(
        paper_id=pid,
        title=title,
        abstract=abstract,
        authors=("Researcher",),
        year=2026,
        doi=doi,
        url="https://example.org/" + pid.replace(":", "-"),
        open_access_pdf_url=None,
        retrieved_at="2026-09-21T00:00:00+00:00",
        provider=provider,
        providers=(provider,),
    )


class ResearchMeshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_catalog_has_free_biomedical_sources_and_gemini_web_tools(self):
        pubmed = get_provider("pubmed")
        europe = get_provider("europe_pmc")
        gemini = get_provider("gemini")
        self.assertEqual(pubmed.cost_class, "free")
        self.assertEqual(europe.cost_class, "free")
        self.assertIn("biomedical_search", pubmed.capabilities)
        self.assertIn("biomedical_search", europe.capabilities)
        self.assertIn("google_search_grounding", gemini.capabilities)
        self.assertIn("url_context", gemini.capabilities)

    def test_biomedical_mesh_is_free_first_and_ready_without_keys(self):
        plan = research_mesh_plan(self.root, "biomedical", environ={})
        chain = plan["selected_provider_ids"] + plan["fallback_provider_ids"]
        self.assertEqual(set(chain), {"europe_pmc", "pubmed"})
        self.assertEqual(plan["ready_capability_count"], 1)
        self.assertFalse(plan["paid_spending"])

    def test_general_web_does_not_auto_enable_metered_google_or_tavily(self):
        plan = research_mesh_plan(
            self.root,
            "general_web",
            allow_metered=False,
            environ={"GEMINI_API_KEY": "configured", "TAVILY_API_KEY": "configured"},
        )
        self.assertEqual(plan["ready_capability_count"], 0)
        self.assertFalse(plan["paid_spending"])
        self.assertFalse(plan["access_request_created"])

    def test_biomedical_search_dedupes_cross_provider_doi_and_caches(self):
        europe = FakeProvider(
            "europe_pmc",
            [make_paper("europe_pmc", "europe_pmc:MED:1", "Shared", "10.1/shared", "Abstract")],
        )
        pubmed = FakeProvider(
            "pubmed",
            [make_paper("pubmed", "pubmed:1", "Shared", "10.1/shared", None)],
        )
        first = search_biomedical_free(
            self.root,
            "biomedical evidence",
            providers=(europe, pubmed),
        )
        self.assertEqual(len(first["results"]), 1)
        self.assertEqual(
            set(first["results"][0]["providers"]),
            {"europe_pmc", "pubmed"},
        )
        before = (europe.calls, pubmed.calls)
        second = search_biomedical_free(
            self.root,
            "biomedical evidence",
            providers=(europe, pubmed),
        )
        self.assertEqual((europe.calls, pubmed.calls), before)
        self.assertTrue(second["metrics"]["cache_hit"])


if __name__ == "__main__":
    unittest.main()
