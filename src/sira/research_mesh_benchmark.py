"""Offline benchmark for Research Mesh v1.7A."""
from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

from .papers import Paper, PaperBatch
from .provider_catalog import get_provider
from .research_mesh import research_mesh_plan, search_biomedical_free
from .storage import RunStore, write_json


class _FakeProvider:
    def __init__(self, name: str, papers: tuple[Paper, ...]):
        self.name = name
        self.cache_namespace = "fake-" + name
        self.papers = papers
        self.calls = 0

    def search(self, query: str, max_results: int) -> PaperBatch:
        self.calls += 1
        return PaperBatch(
            self.papers[:max_results],
            api_requests=0,
            providers_attempted=(self.name,),
        )


def _paper(provider: str, paper_id: str, title: str, doi: str | None, abstract: str | None):
    return Paper(
        paper_id=paper_id,
        title=title,
        abstract=abstract,
        authors=("Researcher",),
        year=2026,
        doi=doi,
        url="https://example.org/" + paper_id.replace(":", "-"),
        open_access_pdf_url=None,
        retrieved_at="2026-09-21T00:00:00+00:00",
        provider=provider,
        providers=(provider,),
    )


def research_mesh_benchmark(root: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(root).resolve()
    cases: list[dict[str, object]] = []

    pubmed = get_provider("pubmed")
    europe = get_provider("europe_pmc")
    gemini = get_provider("gemini")

    cases.append({
        "case_id": "pubmed_free_optional_key",
        "passed": pubmed.cost_class == "free" and pubmed.access == "optional_key",
    })
    cases.append({
        "case_id": "europe_pmc_public_free",
        "passed": europe.cost_class == "free" and europe.access == "public",
    })
    cases.append({
        "case_id": "gemini_web_tools_cataloged",
        "passed": (
            "google_search_grounding" in gemini.capabilities
            and "url_context" in gemini.capabilities
        ),
    })

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        biomedical = research_mesh_plan(sandbox, "biomedical", environ={})
        cases.append({
            "case_id": "biomedical_mesh_is_free_ready",
            "passed": (
                biomedical["ready_capability_count"] == 1
                and set(biomedical["selected_provider_ids"] + biomedical["fallback_provider_ids"])
                == {"europe_pmc", "pubmed"}
                and biomedical["paid_spending"] is False
            ),
        })

        general = research_mesh_plan(
            sandbox,
            "general_web",
            allow_metered=False,
            environ={"GEMINI_API_KEY": "present", "TAVILY_API_KEY": "present"},
        )
        cases.append({
            "case_id": "general_web_metered_lanes_disabled_by_default",
            "passed": (
                general["ready_capability_count"] == 0
                and general["paid_spending"] is False
                and general["access_request_created"] is False
            ),
        })

        a = _FakeProvider(
            "europe_pmc",
            (_paper("europe_pmc", "europe_pmc:MED:1", "Shared biomedical work", "10.1/shared", "Rich abstract"),),
        )
        b = _FakeProvider(
            "pubmed",
            (_paper("pubmed", "pubmed:1", "Shared biomedical work", "10.1/shared", None),),
        )
        report = search_biomedical_free(
            sandbox,
            "biomedical evidence",
            providers=(a, b),
            use_cache=True,
        )
        cases.append({
            "case_id": "cross_provider_dedup",
            "passed": (
                len(report["results"]) == 1
                and set(report["results"][0]["providers"]) == {"europe_pmc", "pubmed"}
            ),
        })
        cases.append({
            "case_id": "biomedical_execution_never_spends",
            "passed": (
                report["paid_spending"] is False
                and report["metered_provider_requests"] == 0
            ),
        })

        before = (a.calls, b.calls)
        cached = search_biomedical_free(
            sandbox,
            "biomedical evidence",
            providers=(a, b),
            use_cache=True,
        )
        cases.append({
            "case_id": "biomedical_cache_prevents_second_provider_call",
            "passed": (
                (a.calls, b.calls) == before
                and cached["metrics"]["cache_hit"] is True
            ),
        })

        cases.append({
            "case_id": "mesh_is_non_authoritative",
            "passed": (
                biomedical["authority_granted"] is False
                and biomedical["promotion_authorized"] is False
                and biomedical["provider_execution_performed"] is False
            ),
        })

        try:
            research_mesh_plan(sandbox, "unknown-domain")
        except ValueError:
            unknown_ok = True
        else:
            unknown_ok = False
        cases.append({
            "case_id": "unknown_domain_fails_closed",
            "passed": unknown_ok,
        })

    passed = sum(bool(row["passed"]) for row in cases)
    report = {
        "schema_version": 1,
        "kind": "research_mesh_benchmark",
        "suite_id": "sira-research-mesh-v1.7a",
        "passed": passed,
        "failed": len(cases) - passed,
        "api_requests": 0,
        "cases": cases,
    }
    store = RunStore(root)
    path = store.path / "research-mesh-benchmark.json"
    write_json(path, report)
    return path, report
