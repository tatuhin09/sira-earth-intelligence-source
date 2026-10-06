"""Offline benchmark for Gemini research tools and zero-cost guard."""
from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

from .gemini_research_runtime import research_cost_guard, run_gemini_research
from .papers import Paper
from .providers.gemini_research import (
    MODE_GOOGLE_SEARCH,
    MODE_URL_CONTEXT,
    GeminiResearchBatch,
    GroundedSource,
)
from .storage import RunStore, write_json


class _FakeModel:
    def __init__(self):
        self.calls = 0

    def research(self, query, *, urls=(), mode):
        self.calls += 1
        return GeminiResearchBatch(
            text="Grounded result",
            sources=(
                GroundedSource("https://example.org/source", "Example", "google_search", 0),
            ),
            web_search_queries=("example query",) if mode == MODE_GOOGLE_SEARCH else (),
            url_retrievals=(
                {"url": urls[0], "status": "URL_RETRIEVAL_STATUS_SUCCESS"},
            ) if urls else (),
            grounding_supports=(),
            api_requests=1,
            input_tokens=10,
            output_tokens=5,
            tool_mode=mode,
        )


def gemini_research_benchmark(root: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(root).resolve()
    cases = []
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)

        default_url = research_cost_guard(sandbox, MODE_URL_CONTEXT, environ={})
        cases.append({
            "case_id": "url_context_default_denied",
            "passed": not default_url["allowed"],
        })

        confirmed_url = research_cost_guard(
            sandbox,
            MODE_URL_CONTEXT,
            environ={"SIRA_GEMINI_FREE_TIER_CONFIRMED": "true"},
        )
        cases.append({
            "case_id": "url_context_owner_free_tier_confirmation",
            "passed": confirmed_url["allowed"],
        })

        search_missing = research_cost_guard(
            sandbox,
            MODE_GOOGLE_SEARCH,
            environ={"SIRA_GEMINI_FREE_TIER_CONFIRMED": "true"},
        )
        cases.append({
            "case_id": "search_requires_separate_zero_cost_confirmation",
            "passed": not search_missing["allowed"],
        })

        search_ready = research_cost_guard(
            sandbox,
            MODE_GOOGLE_SEARCH,
            environ={
                "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
                "SIRA_GEMINI_SEARCH_ZERO_COST_CONFIRMED": "true",
            },
        )
        cases.append({
            "case_id": "search_confirmation_still_never_authorizes_payment",
            "passed": (
                search_ready["allowed"]
                and search_ready["paid_spending_authorized"] is False
                and search_ready["billing_changes_authorized"] is False
            ),
        })

        blocked = run_gemini_research(
            sandbox,
            "research",
            urls=("https://example.org/",),
            mode=MODE_URL_CONTEXT,
            environ={},
            model=_FakeModel(),
        )
        cases.append({
            "case_id": "blocked_run_has_zero_api_requests",
            "passed": (
                blocked["status"] == "blocked"
                and blocked["metrics"]["api_requests"] == 0
            ),
        })

        # Broker requires configured-key presence. A presence-only env value is
        # sufficient for this offline benchmark because the fake model gets injected.
        fake = _FakeModel()
        completed = run_gemini_research(
            sandbox,
            "research",
            urls=("https://example.org/",),
            mode=MODE_URL_CONTEXT,
            environ={
                "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
                "GEMINI_API_KEY": "configured",
            },
            model=fake,
        )
        cases.append({
            "case_id": "confirmed_url_context_executes_injected_model",
            "passed": completed["status"] == "completed" and fake.calls == 1,
        })
        cases.append({
            "case_id": "completed_run_is_non_authoritative",
            "passed": (
                completed["paid_spending"] is False
                and completed["authority_granted"] is False
                and completed["promotion_authorized"] is False
            ),
        })

        before = fake.calls
        cached = run_gemini_research(
            sandbox,
            "research",
            urls=("https://example.org/",),
            mode=MODE_URL_CONTEXT,
            environ={
                "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
                "GEMINI_API_KEY": "configured",
            },
            model=fake,
        )
        cases.append({
            "case_id": "cache_avoids_second_tool_call",
            "passed": cached["metrics"]["cache_hit"] is True and fake.calls == before,
        })

    passed = sum(bool(row["passed"]) for row in cases)
    report = {
        "schema_version": 1,
        "kind": "gemini_research_benchmark",
        "suite_id": "sira-gemini-research-v1.7b",
        "passed": passed,
        "failed": len(cases) - passed,
        "api_requests": 0,
        "cases": cases,
    }
    store = RunStore(root)
    path = store.path / "gemini-research-benchmark.json"
    write_json(path, report)
    return path, report
