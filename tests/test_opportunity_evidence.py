import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.cli import main


class OpportunityEvidenceTests(unittest.TestCase):
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

    def _discover_target(self, path="src/sira/paper_reading.py", symbol="paper_read_run"):
        from sira.opportunity import discover_opportunities
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._complex_function(symbol), encoding="utf-8")
        result = discover_opportunities(self.root, limit=20, now_epoch=2_000_000_000)
        return next(row for row in result["opportunities"] if row.get("symbol") == symbol)

    def test_evidence_collects_exact_target_callers_tests_and_benchmark_mapping(self):
        from sira.opportunity_evidence import build_opportunity_evidence

        opportunity = self._discover_target()
        (self.root / "src/sira/helper.py").write_text(
            "from .paper_reading import paper_read_run\n\ndef caller(x):\n    return paper_read_run(x)\n",
            encoding="utf-8",
        )
        (self.root / "tests/test_paper_reading.py").write_text(
            "from sira.paper_reading import paper_read_run\n\ndef test_paper_read_run_contract():\n    assert callable(paper_read_run)\n",
            encoding="utf-8",
        )

        report = build_opportunity_evidence(self.root, opportunity["opportunity_id"])

        self.assertEqual(report["kind"], "opportunity_evidence_brief")
        self.assertEqual(report["target"]["path"], "src/sira/paper_reading.py")
        self.assertEqual(report["target"]["symbol"], "paper_read_run")
        self.assertIn("def paper_read_run", report["target"]["source_excerpt"])
        self.assertTrue(any(row["path"] == "src/sira/helper.py" for row in report["callers"]))
        self.assertTrue(any(row["path"] == "tests/test_paper_reading.py" for row in report["tests"]))
        self.assertEqual(report["benchmark"]["suite"], "paper-reading")
        self.assertEqual(report["benchmark"]["capability"], "paper_reading")
        self.assertEqual(report["assessment"]["decision"], "research_ready")
        self.assertEqual(report["api_requests"], 0)
        self.assertFalse(report["paid_spending"])
        self.assertTrue(Path(report["artifact"]).is_file())

    def test_stale_opportunity_is_rejected_when_source_changes(self):
        from sira.opportunity_evidence import build_opportunity_evidence

        opportunity = self._discover_target()
        path = self.root / "src/sira/paper_reading.py"
        path.write_text(path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "stale"):
            build_opportunity_evidence(self.root, opportunity["opportunity_id"])

    def test_weak_unmapped_opportunity_is_not_sent_forward(self):
        from sira.opportunity_evidence import build_opportunity_evidence

        opportunity = self._discover_target(path="src/sira/misc_tool.py", symbol="hard_path")
        report = build_opportunity_evidence(self.root, opportunity["opportunity_id"])

        self.assertIsNone(report["benchmark"]["suite"])
        self.assertEqual(report["assessment"]["decision"], "skip_weak_evidence")
        self.assertFalse(report["success_criteria"]["research_ready"])

    def test_related_memory_is_local_and_synthetic_provider_is_excluded(self):
        from sira.memory import MemoryStore, Observation
        from sira.opportunity_evidence import build_opportunity_evidence

        opportunity = self._discover_target()
        store = MemoryStore(self.root)
        store.upsert(Observation(
            "failure", "bad_source", "paper_reading", "real paper reading failure", "observed",
            provider="bounded_pdf_text", error_code="no_readable_content", signature="real"
        ), run_id="run_real", artifact_name="evidence.json", artifact_sha256="a" * 64, outcome_status="partial")
        store.upsert(Observation(
            "failure", "bad_source", "paper_reading", "fixture paper reading failure", "observed",
            provider="paper_fixture", error_code="no_readable_content", signature="fixture"
        ), run_id="run_fixture", artifact_name="evidence.json", artifact_sha256="b" * 64, outcome_status="partial")

        report = build_opportunity_evidence(self.root, opportunity["opportunity_id"])
        providers = {row.get("provider") for row in report["related_memories"]}
        self.assertIn("bounded_pdf_text", providers)
        self.assertNotIn("paper_fixture", providers)
        self.assertEqual(report["api_requests"], 0)

    def test_success_criteria_are_measurable_for_complex_function(self):
        from sira.opportunity_evidence import build_opportunity_evidence

        opportunity = self._discover_target()
        (self.root / "tests/test_paper_reading.py").write_text("from sira.paper_reading import paper_read_run\n", encoding="utf-8")
        report = build_opportunity_evidence(self.root, opportunity["opportunity_id"])
        structural = report["success_criteria"]["structural_goal"]

        self.assertEqual(structural["metric"], "branch_points")
        self.assertEqual(structural["baseline"], opportunity["evidence"]["branch_points"])
        self.assertLess(structural["target_max"], structural["baseline"])
        self.assertTrue(report["success_criteria"]["unit_tests_must_pass"])
        self.assertTrue(report["success_criteria"]["test_count_must_not_decrease"])
        self.assertEqual(report["success_criteria"]["benchmark_suite"], "paper-reading")


    def test_comments_and_strings_do_not_count_as_test_coverage(self):
        from sira.opportunity_evidence import build_opportunity_evidence

        opportunity = self._discover_target()
        (self.root / "tests/test_noise.py").write_text(
            "# paper_read_run is mentioned only in a comment\nNAME = \"paper_read_run\"\n",
            encoding="utf-8",
        )
        report = build_opportunity_evidence(self.root, opportunity["opportunity_id"])

        self.assertFalse(any(row["path"] == "tests/test_noise.py" for row in report["tests"]))
        self.assertEqual(report["assessment"]["decision"], "skip_weak_evidence")

    def test_cli_improve_evidence_outputs_json_without_network(self):
        opportunity = self._discover_target()
        (self.root / "tests/test_paper_reading.py").write_text("from sira.paper_reading import paper_read_run\n", encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--root", str(self.root), "improve", "evidence", opportunity["opportunity_id"]])

        self.assertEqual(code, 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["kind"], "opportunity_evidence_brief")
        self.assertEqual(data["api_requests"], 0)
        self.assertEqual(data["opportunity"]["opportunity_id"], opportunity["opportunity_id"])


    def test_related_memories_use_ranked_autonomous_retrieval_and_record_usage(self):
        from unittest.mock import patch
        import sira.opportunity_evidence as opportunity_evidence
        from sira.memory import MemoryStore, Observation

        store = MemoryStore(self.root)
        memory_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "validated opportunity retrieval lesson", "validated",
                provider="tavily",
                signature="opportunity-consumer-integration",
            ),
            run_id="93" * 16, artifact_name="result.json",
            artifact_sha256="3" * 64, outcome_status="completed",
        )

        calls = []

        def fake_retrieve(instance, query, limit=20, *, autonomous=False):
            calls.append((query, limit, autonomous))
            row = instance.show(memory_id)
            row["retrieval_score"] = 0.88
            row["score_reasons"] = [
                {"component": "relevance", "effect": "boost", "value": 0.45},
                {"component": "confidence", "effect": "boost", "value": 0.29},
                {"component": "freshness", "effect": "boost", "value": 0.08},
            ]
            return [row]

        with patch.object(MemoryStore, "retrieve", new=fake_retrieve), \
             patch.object(
                 MemoryStore,
                 "search",
                 side_effect=AssertionError(
                     "opportunity memory context must not use legacy search()"
                 ),
             ):
            related = opportunity_evidence._related_memories(
                self.root, "retrieval"
            )

        self.assertEqual(calls, [("retrieval", 20, True)])
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["memory_id"], memory_id)
        self.assertEqual(related[0]["retrieval_score"], 0.88)
        self.assertTrue(related[0]["score_reasons"])

        outcomes = MemoryStore(self.root).outcomes(memory_id)
        used = [
            row for row in outcomes
            if row["outcome_type"] == "retrieval_used"
            and row["context"].get("consumer")
               == "opportunity_evidence._related_memories"
        ]
        self.assertEqual(len(used), 1)
        self.assertEqual(used[0]["context"]["query"], "retrieval")


if __name__ == "__main__":
    unittest.main()
