import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.cli import main
from sira.models import ProviderError, utc_now
from sira.papers import Paper, PaperBatch


class FakePaperProvider:
    def __init__(self, name, papers=(), error=None):
        self.name = name
        self.cache_namespace = "fake:" + name
        self._papers = tuple(papers)
        self._error = error
        self.calls = []

    def search(self, query, max_results):
        self.calls.append((query, max_results))
        if self._error is not None:
            raise self._error
        return PaperBatch(self._papers[:max_results], api_requests=1,
                          providers_attempted=(self.name,))


class OpportunityFreeResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for rel in ("src/sira", "tests", "benchmarks", "docs"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        (self.root / "sira.py").write_text("print('fixture')\n", encoding="utf-8")
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")
        (self.root / ".env.example").write_text("\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _complex_function(name="paper_read_run"):
        lines = [f"def {name}(value):"]
        for idx in range(20):
            lines.extend([f"    if value == {idx}:", f"        value += {idx + 1}"])
        lines.extend(["    return value", ""])
        return "\n".join(lines)

    def _evidence(self):
        from sira.opportunity import discover_opportunities
        from sira.opportunity_evidence import build_opportunity_evidence

        target = self.root / "src/sira/paper_reading.py"
        target.write_text(self._complex_function(), encoding="utf-8")
        (self.root / "tests/test_paper_reading.py").write_text(
            "from sira.paper_reading import paper_read_run\n\ndef test_contract():\n    assert callable(paper_read_run)\n",
            encoding="utf-8",
        )
        opportunity = discover_opportunities(self.root, limit=20, now_epoch=2_000_000_000)["opportunities"][0]
        return build_opportunity_evidence(self.root, opportunity["opportunity_id"])

    @staticmethod
    def _paper(pid, title, abstract, url, provider, doi=None):
        return Paper(
            paper_id=pid,
            title=title,
            abstract=abstract,
            authors=("Researcher",),
            year=2024,
            doi=doi,
            url=url,
            open_access_pdf_url=None,
            retrieved_at=utc_now(),
            provider=provider,
        )

    def test_free_research_uses_public_providers_and_builds_cited_writer_ready_brief(self):
        from sira.opportunity_research import research_opportunity_free

        evidence = self._evidence()
        arxiv = FakePaperProvider("arxiv", (
            self._paper("a1", "Refactoring Complex Functions", "Extract method can reduce complex control flow while preserving behavior.",
                        "https://arxiv.org/abs/2401.00001", "arxiv"),
        ))
        crossref = FakePaperProvider("crossref", (
            self._paper("c1", "Software Refactoring and Maintainability", "Empirical study of refactoring and maintainability.",
                        "https://doi.org/10.1000/example", "crossref", "10.1000/example"),
        ))

        report = research_opportunity_free(self.root, evidence["evidence_id"], providers=(arxiv, crossref), use_cache=False)

        self.assertEqual(report["kind"], "opportunity_free_research_brief")
        self.assertEqual(report["evidence_id"], evidence["evidence_id"])
        self.assertEqual(report["research_quality"]["decision"], "writer_ready")
        self.assertTrue(report["writer_handoff_allowed"])
        self.assertEqual(report["api_requests"], 2)
        self.assertEqual(report["metered_model_requests"], 0)
        self.assertFalse(report["paid_spending"])
        self.assertEqual([row["citation_id"] for row in report["citations"]], ["R1", "R2"])
        self.assertEqual({row["provider"] for row in report["citations"]}, {"arxiv", "crossref"})
        self.assertTrue(all(row["url"].startswith("https://") for row in report["citations"]))
        self.assertTrue(report["candidate_strategies"])
        self.assertTrue(Path(report["artifact"]).is_file())
        self.assertIn("refactoring", arxiv.calls[0][0].casefold())
        self.assertEqual(arxiv.calls[0][1], 3)

    def test_free_research_deduplicates_same_work_and_requires_meaningful_external_evidence(self):
        from sira.opportunity_research import research_opportunity_free

        evidence = self._evidence()
        p1 = self._paper("a1", "Same Refactoring Study", "Useful abstract about refactoring.",
                         "https://arxiv.org/abs/2401.00002", "arxiv", "10.1000/same")
        p2 = self._paper("c1", "Same Refactoring Study", "Duplicate metadata.",
                         "https://doi.org/10.1000/same", "crossref", "10.1000/same")
        report = research_opportunity_free(
            self.root, evidence["evidence_id"],
            providers=(FakePaperProvider("arxiv", (p1,)), FakePaperProvider("crossref", (p2,))),
            use_cache=False,
        )

        self.assertEqual(len(report["citations"]), 1)
        self.assertEqual(report["research_quality"]["decision"], "insufficient_external_evidence")
        self.assertFalse(report["writer_handoff_allowed"])

    def test_provider_failure_is_recorded_without_aborting_other_free_source(self):
        from sira.opportunity_research import research_opportunity_free

        evidence = self._evidence()
        good = self._paper("c1", "Refactoring Study", "A useful external abstract.",
                           "https://doi.org/10.1000/good", "crossref", "10.1000/good")
        arxiv = FakePaperProvider("arxiv", error=ProviderError("http_429", True, retry_after=7, request_count=1))
        crossref = FakePaperProvider("crossref", (good,))

        report = research_opportunity_free(self.root, evidence["evidence_id"], providers=(arxiv, crossref), use_cache=False)

        self.assertEqual(report["api_requests"], 2)
        self.assertEqual(report["provider_failures"][0]["provider"], "arxiv")
        self.assertEqual(report["provider_failures"][0]["code"], "http_429")
        self.assertEqual(report["provider_failures"][0]["retry_after_seconds"], 7)
        self.assertEqual(report["research_quality"]["decision"], "insufficient_external_evidence")
        self.assertFalse(report["writer_handoff_allowed"])

    def test_cache_reuses_public_research_without_second_provider_call(self):
        from sira.opportunity_research import research_opportunity_free

        evidence = self._evidence()
        papers = (
            self._paper("a1", "Refactoring One", "Useful abstract one.", "https://arxiv.org/abs/2401.1", "arxiv"),
            self._paper("a2", "Refactoring Two", "Useful abstract two.", "https://arxiv.org/abs/2401.2", "arxiv"),
        )
        provider = FakePaperProvider("arxiv", papers)

        first = research_opportunity_free(self.root, evidence["evidence_id"], providers=(provider,), use_cache=True)
        second = research_opportunity_free(self.root, evidence["evidence_id"], providers=(provider,), use_cache=True)

        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["api_requests"], 0)
        self.assertEqual(second["citations"], first["citations"])

    def test_stale_evidence_is_rejected_before_any_network_request(self):
        from sira.opportunity_research import research_opportunity_free

        evidence = self._evidence()
        target = self.root / "src/sira/paper_reading.py"
        target.write_text(target.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        provider = FakePaperProvider("arxiv", ())

        with self.assertRaisesRegex(ValueError, "stale"):
            research_opportunity_free(self.root, evidence["evidence_id"], providers=(provider,), use_cache=False)
        self.assertEqual(provider.calls, [])

    def test_cli_free_research_outputs_json_contract(self):
        fake = {
            "kind": "opportunity_free_research_brief",
            "research_quality": {"decision": "writer_ready"},
            "api_requests": 1,
            "paid_spending": False,
        }
        output = io.StringIO()
        with patch("sira.cli.research_opportunity_free", return_value=fake) as call:
            with redirect_stdout(output):
                code = main(["--root", str(self.root), "improve", "free-research", "oe_" + "a" * 32])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), fake)
        call.assert_called_once()


    def test_richer_duplicate_from_openalex_recovers_abstract_and_provider_provenance(self):
        from sira.opportunity_research import research_opportunity_free

        evidence = self._evidence()
        crossref = self._paper(
            "c1", "Shared Refactoring Study", None,
            "https://doi.org/10.1000/shared", "crossref", "10.1000/shared"
        )
        openalex_rich = self._paper(
            "o1", "Shared Refactoring Study", "A useful abstract about behavior-preserving refactoring.",
            "https://openalex.org/W1", "openalex", "10.1000/shared"
        )
        openalex_second = self._paper(
            "o2", "Another Refactoring Study", "Independent evidence about maintainable extraction.",
            "https://openalex.org/W2", "openalex", "10.1000/second"
        )
        report = research_opportunity_free(
            self.root,
            evidence["evidence_id"],
            providers=(FakePaperProvider("crossref", (crossref,)), FakePaperProvider("openalex", (openalex_rich, openalex_second))),
            use_cache=False,
        )

        self.assertEqual(report["research_quality"]["decision"], "writer_ready")
        self.assertTrue(report["writer_handoff_allowed"])
        self.assertEqual(len(report["citations"]), 2)
        shared = next(row for row in report["citations"] if row["doi"] == "10.1000/shared")
        self.assertIn("useful abstract", shared["abstract_excerpt"])
        self.assertEqual(set(shared["providers"]), {"crossref", "openalex"})
        self.assertEqual(report["research_quality"]["provider_count"], 2)

    def test_transient_provider_failure_enters_cooldown_and_is_skipped_on_next_uncached_run(self):
        from sira.opportunity_research import research_opportunity_free

        evidence = self._evidence()
        arxiv = FakePaperProvider("arxiv", error=ProviderError("http_429", True, request_count=1))
        crossref = FakePaperProvider("crossref", ())
        openalex = FakePaperProvider("openalex", ())

        with patch("sira.opportunity_research.time.time", return_value=1_000.0):
            first = research_opportunity_free(
                self.root, evidence["evidence_id"], providers=(arxiv, crossref, openalex), use_cache=False
            )
        with patch("sira.opportunity_research.time.time", return_value=1_010.0):
            second = research_opportunity_free(
                self.root, evidence["evidence_id"], providers=(arxiv, crossref, openalex), use_cache=False
            )

        self.assertEqual(len(arxiv.calls), 1)
        self.assertEqual(len(crossref.calls), 2)
        self.assertEqual(len(openalex.calls), 2)
        self.assertEqual(first["provider_failures"][0]["provider"], "arxiv")
        self.assertEqual(second["providers_attempted"], ["crossref", "openalex"])
        self.assertEqual(second["providers_skipped_cooldown"][0]["provider"], "arxiv")
        self.assertEqual(second["providers_skipped_cooldown"][0]["code"], "http_429")
        self.assertGreater(second["providers_skipped_cooldown"][0]["remaining_seconds"], 0)

    def test_insufficient_transient_failure_result_is_not_cached_for_a_full_day(self):
        from sira.opportunity_research import research_opportunity_free

        evidence = self._evidence()
        arxiv = FakePaperProvider("arxiv", error=ProviderError("http_429", True, request_count=1))
        crossref = FakePaperProvider("crossref", ())
        openalex = FakePaperProvider("openalex", ())

        with patch("sira.opportunity_research.time.time", return_value=5_000.0):
            first = research_opportunity_free(
                self.root, evidence["evidence_id"], providers=(arxiv, crossref, openalex), use_cache=True
            )
        with patch("sira.opportunity_research.time.time", return_value=5_010.0):
            second = research_opportunity_free(
                self.root, evidence["evidence_id"], providers=(arxiv, crossref, openalex), use_cache=True
            )

        self.assertFalse(first["cache_hit"])
        self.assertFalse(second["cache_hit"])
        self.assertEqual(len(crossref.calls), 2)
        self.assertEqual(len(openalex.calls), 2)


    def test_unrelated_abstract_cannot_satisfy_writer_research_gate(self):
        from sira.opportunity_research import research_opportunity_free

        evidence = self._evidence()
        relevant = self._paper(
            "c1", "Refactoring Complex Functions for Maintainability", None,
            "https://doi.org/10.1000/relevant", "crossref", "10.1000/relevant"
        )
        unrelated = self._paper(
            "o1", "Requirements Engineering in Large-Scale Agile Systems",
            "A detailed empirical abstract about requirements engineering, product planning, and agile coordination.",
            "https://openalex.org/W-unrelated", "openalex", "10.1000/unrelated"
        )

        report = research_opportunity_free(
            self.root, evidence["evidence_id"],
            providers=(FakePaperProvider("crossref", (relevant,)), FakePaperProvider("openalex", (unrelated,))),
            use_cache=False,
        )

        self.assertEqual(report["research_quality"]["decision"], "insufficient_external_evidence")
        self.assertFalse(report["writer_handoff_allowed"])
        self.assertEqual([row["title"] for row in report["citations"]], [relevant.title])
        self.assertEqual(report["research_quality"]["rejected_irrelevant_count"], 1)
        self.assertEqual(report["rejected_irrelevant_sources"][0]["title"], unrelated.title)
        self.assertEqual(report["rejected_irrelevant_sources"][0]["provider"], "openalex")

    def test_relevance_gate_keeps_refactoring_sources_and_reports_local_scores(self):
        from sira.opportunity_research import research_opportunity_free

        evidence = self._evidence()
        crossref = self._paper(
            "c1", "Identifying Refactoring Sequences for Improving Software Maintainability", None,
            "https://doi.org/10.1000/refactor-one", "crossref", "10.1000/refactor-one"
        )
        openalex = self._paper(
            "o1", "A Review of Refactoring for Code Smells",
            "Refactoring and extract-method techniques can reduce code smells while preserving observable behavior.",
            "https://openalex.org/W-refactor", "openalex", "10.1000/refactor-two"
        )

        report = research_opportunity_free(
            self.root, evidence["evidence_id"],
            providers=(FakePaperProvider("crossref", (crossref,)), FakePaperProvider("openalex", (openalex,))),
            use_cache=False,
        )

        self.assertEqual(report["research_quality"]["decision"], "writer_ready")
        self.assertTrue(report["writer_handoff_allowed"])
        self.assertEqual(report["research_quality"]["relevant_source_count"], 2)
        self.assertEqual(report["research_quality"]["relevant_abstract_count"], 1)
        self.assertEqual(report["research_quality"]["rejected_irrelevant_count"], 0)
        self.assertTrue(all(row["relevance"]["eligible"] for row in report["citations"]))
        self.assertTrue(all(row["relevance"]["matched_terms"] for row in report["citations"]))



if __name__ == "__main__":
    unittest.main()
