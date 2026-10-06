from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

# SIRA_TEST_SRC_PATH_FIX_V1
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SRC_TEXT = str(SRC)
sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]

from sira.memory import MemoryStore
from sira.models import ProviderError, utc_now
from sira.opportunity_research import research_opportunity_free
from sira.papers import Paper, PaperBatch
from sira.storage import write_json


class _SuccessProvider:
    name = "openalex"
    cache_namespace = "test:openalex:success"

    def search(self, query: str, max_results: int) -> PaperBatch:
        paper = Paper(
            paper_id="oa-test-1",
            title="Software refactoring maintainability and characterization tests",
            abstract=(
                "Refactoring complex functions while preserving behavior can improve "
                "maintainability when regression and characterization tests are retained."
            ),
            authors=("Test Author",),
            year=2025,
            doi="10.0000/test",
            url="https://example.org/paper",
            open_access_pdf_url=None,
            retrieved_at=utc_now(),
            provider=self.name,
            providers=(self.name,),
        )
        return PaperBatch(
            (paper,),
            api_requests=1,
            providers_attempted=(self.name,),
        )


class _EmptyProvider:
    name = "openalex"
    cache_namespace = "test:openalex:empty"

    def search(self, query: str, max_results: int) -> PaperBatch:
        return PaperBatch(
            (),
            api_requests=1,
            providers_attempted=(self.name,),
        )


class _FailProvider:
    name = "openalex"
    cache_namespace = "test:openalex:failure"

    def search(self, query: str, max_results: int) -> PaperBatch:
        raise ProviderError("http_503", True, request_count=1)


class ProviderOutcomeFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)
        source = self.root / "src/sira/demo_target.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "def demo_target(value):\n"
            "    if value:\n"
            "        return value\n"
            "    return None\n",
            encoding="utf-8",
        )
        payload = source.read_bytes()
        self.evidence_id = "oe_" + "1" * 32
        evidence = {
            "schema_version": 1,
            "kind": "opportunity_evidence_brief",
            "evidence_id": self.evidence_id,
            "assessment": {"decision": "research_ready"},
            "opportunity": {
                "opportunity_id": "op_" + "2" * 32,
                "path": "src/sira/demo_target.py",
                "symbol": "demo_target",
                "source_sha256": hashlib.sha256(payload).hexdigest(),
                "fingerprint": "feedback-test-fingerprint",
                "type": "complex_function",
            },
            "target": {
                "path": "src/sira/demo_target.py",
                "symbol": "demo_target",
            },
            "success_criteria": {
                "structural_goal": {
                    "metric": "cyclomatic_complexity",
                },
                "benchmark_suite": "strategy-feedback-test",
            },
        }
        write_json(
            self.root
            / "improvements/opportunities/evidence"
            / f"{self.evidence_id}.json",
            evidence,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _strategy_outcomes(self):
        store = MemoryStore(self.root)
        found = []
        for row in store.search("paper_search", 50):
            for outcome in store.outcomes(row["memory_id"]):
                context = outcome.get("context")
                if (
                    isinstance(context, dict)
                    and context.get("consumer") == "opportunity_research.provider_feedback"
                ):
                    found.append((row, outcome))
        return found

    def test_useful_provider_result_records_application_succeeded(self):
        report = research_opportunity_free(
            self.root,
            self.evidence_id,
            providers=(_SuccessProvider(),),
            use_cache=False,
        )
        self.assertEqual(report["providers_attempted"], ["openalex"])

        outcomes = self._strategy_outcomes()
        self.assertEqual(len(outcomes), 1)
        memory, outcome = outcomes[0]
        self.assertEqual(memory["provider"], "openalex")
        self.assertEqual(outcome["outcome_type"], "application_succeeded")
        self.assertEqual(
            outcome["context"],
            {
                "consumer": "opportunity_research.provider_feedback",
                "strategy_id": "openalex",
                "capability": "paper_search",
                "evidence_id": self.evidence_id,
            },
        )

    def test_provider_error_records_application_failed_without_raw_error(self):
        report = research_opportunity_free(
            self.root,
            self.evidence_id,
            providers=(_FailProvider(),),
            use_cache=False,
        )
        self.assertEqual(report["provider_failures"][0]["code"], "http_503")

        outcomes = self._strategy_outcomes()
        self.assertEqual(len(outcomes), 1)
        memory, outcome = outcomes[0]
        self.assertEqual(memory["provider"], "openalex")
        self.assertEqual(outcome["outcome_type"], "application_failed")
        self.assertEqual(
            set(outcome["context"]),
            {"consumer", "strategy_id", "capability", "evidence_id"},
        )
        rendered = repr(outcome)
        self.assertNotIn("raw_error", rendered)
        self.assertNotIn("traceback", rendered.casefold())

    def test_empty_nonerror_result_does_not_create_negative_feedback(self):
        report = research_opportunity_free(
            self.root,
            self.evidence_id,
            providers=(_EmptyProvider(),),
            use_cache=False,
        )
        self.assertEqual(report["providers_attempted"], ["openalex"])
        self.assertEqual(self._strategy_outcomes(), [])

    def test_cache_hit_does_not_duplicate_provider_attempt_feedback(self):
        first = research_opportunity_free(
            self.root,
            self.evidence_id,
            providers=(_SuccessProvider(),),
            use_cache=True,
        )
        self.assertFalse(first["cache_hit"])
        self.assertEqual(len(self._strategy_outcomes()), 1)

        second = research_opportunity_free(
            self.root,
            self.evidence_id,
            providers=(_SuccessProvider(),),
            use_cache=True,
        )
        self.assertTrue(second["cache_hit"])
        self.assertEqual(len(self._strategy_outcomes()), 1)


if __name__ == "__main__":
    unittest.main()
