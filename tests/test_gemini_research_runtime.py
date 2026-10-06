from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [item for item in sys.path if item != str(SRC)]

from sira.gemini_research_runtime import research_cost_guard, run_gemini_research
from sira.providers.gemini_research import (
    MODE_GOOGLE_SEARCH,
    MODE_URL_CONTEXT,
    GeminiResearchBatch,
)


class FakeModel:
    def __init__(self):
        self.calls = 0

    def research(self, query, *, urls=(), mode):
        self.calls += 1
        return GeminiResearchBatch(
            text="Research result",
            sources=(),
            web_search_queries=(),
            url_retrievals=(
                {"url": urls[0], "status": "URL_RETRIEVAL_STATUS_SUCCESS"},
            ) if urls else (),
            grounding_supports=(),
            api_requests=1,
            input_tokens=10,
            output_tokens=5,
            tool_mode=mode,
        )


class GeminiResearchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_zero_cost_guard_is_fail_closed_by_default(self):
        row = research_cost_guard(self.root, MODE_URL_CONTEXT, environ={})
        self.assertFalse(row["allowed"])
        self.assertEqual(row["reason"], "free_tier_confirmation_missing")
        self.assertFalse(row["paid_spending_authorized"])

    def test_search_requires_separate_confirmation_and_has_local_cap(self):
        first = research_cost_guard(
            self.root,
            MODE_GOOGLE_SEARCH,
            environ={"SIRA_GEMINI_FREE_TIER_CONFIRMED": "true"},
        )
        self.assertFalse(first["allowed"])
        ready = research_cost_guard(
            self.root,
            MODE_GOOGLE_SEARCH,
            environ={
                "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
                "SIRA_GEMINI_SEARCH_ZERO_COST_CONFIRMED": "true",
                "SIRA_GEMINI_SEARCH_MONTHLY_CAP": "2",
            },
        )
        self.assertTrue(ready["allowed"])
        self.assertEqual(ready["search_monthly_cap"], 2)
        self.assertFalse(ready["billing_changes_authorized"])

    def test_blocked_run_never_calls_injected_model(self):
        model = FakeModel()
        result = run_gemini_research(
            self.root,
            "research",
            urls=("https://example.org/",),
            mode=MODE_URL_CONTEXT,
            environ={},
            model=model,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(model.calls, 0)
        self.assertEqual(result["metrics"]["api_requests"], 0)

    def test_confirmed_url_context_uses_broker_and_cache(self):
        model = FakeModel()
        env = {
            "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
            "GEMINI_API_KEY": "configured",
        }
        first = run_gemini_research(
            self.root,
            "research",
            urls=("https://example.org/",),
            mode=MODE_URL_CONTEXT,
            environ=env,
            model=model,
        )
        self.assertEqual(first["status"], "completed")
        self.assertEqual(model.calls, 1)
        self.assertFalse(first["paid_spending"])

        second = run_gemini_research(
            self.root,
            "research",
            urls=("https://example.org/",),
            mode=MODE_URL_CONTEXT,
            environ=env,
            model=model,
        )
        self.assertEqual(second["status"], "completed")
        self.assertTrue(second["metrics"]["cache_hit"])
        self.assertEqual(model.calls, 1)


if __name__ == "__main__":
    unittest.main()
