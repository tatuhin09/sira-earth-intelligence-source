import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def project_digest(root: Path) -> str:
    source = root / "src" / "sira"
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*.py")):
        digest.update(str(path.relative_to(source)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


class HistoricalOpportunityIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src" / "sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")
        (self.root / ".env.example").write_text("\n", encoding="utf-8")
        (self.root / "sira.py").write_text("print('fixture')\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _complex_retrieval_module() -> str:
        body = ["def load_evidence_run(value):"]
        for idx in range(14):
            body.extend([f"    if value == {idx}:", f"        value += {idx + 1}"])
        body.extend([f"    value += {idx}" for idx in range(20, 48)])
        body.append("    return value")
        return "\n".join(body) + "\n"

    def _write_current_benchmark(self, *, failed: int, suite_id: str = "sira-retrieval-fixture-v1") -> None:
        run = self.root / "runs" / f"bench_{failed}_{time.time_ns()}"
        run.mkdir(parents=True)
        payload = {
            "schema_version": 1,
            "kind": "synthetic_regression_not_live_quality",
            "suite_id": suite_id,
            "passed": 3 - failed,
            "failed": failed,
            "code_sha256": project_digest(self.root),
            "api_requests": 0,
        }
        (run / "benchmark.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_cross_signal_ranking_boosts_same_symbol_without_network(self):
        from sira.opportunity import discover_opportunities

        (self.root / "src" / "sira" / "retrieval.py").write_text(
            self._complex_retrieval_module(), encoding="utf-8"
        )
        result = discover_opportunities(self.root, limit=20, now_epoch=2_000_000_000)
        rows = [row for row in result["opportunities"] if row.get("symbol") == "load_evidence_run"]

        kinds = {row["type"] for row in rows}
        self.assertIn("complex_function", kinds)
        self.assertIn("missing_test_reference", kinds)
        self.assertTrue(all(row["ranking"]["cross_signal_count"] >= 2 for row in rows))
        self.assertTrue(all(row["ranking"]["cross_signal_boost"] > 0 for row in rows))
        self.assertTrue(all(row["priority_score"] > row["base_priority_score"] for row in rows))
        self.assertEqual(result["api_requests"], 0)
        self.assertEqual(result["paid_spending"], False)

    def test_current_code_benchmark_regression_boosts_related_module_and_stale_report_is_ignored(self):
        from sira.opportunity import discover_opportunities

        path = self.root / "src" / "sira" / "retrieval.py"
        path.write_text(self._complex_retrieval_module(), encoding="utf-8")

        stale = self.root / "runs" / "stale"
        stale.mkdir(parents=True)
        (stale / "benchmark.json").write_text(json.dumps({
            "schema_version": 1,
            "suite_id": "sira-retrieval-fixture-v1",
            "passed": 0,
            "failed": 3,
            "code_sha256": "0" * 64,
            "api_requests": 0,
        }), encoding="utf-8")

        no_current = discover_opportunities(self.root, limit=20, now_epoch=2_000_000_000)
        before = next(row for row in no_current["opportunities"] if row["type"] == "complex_function")
        self.assertEqual(before["ranking"]["benchmark_boost"], 0)

        self._write_current_benchmark(failed=1)
        one_off = discover_opportunities(self.root, limit=20, now_epoch=2_000_000_001)
        one = next(row for row in one_off["opportunities"] if row["type"] == "complex_function")
        self.assertEqual(one["ranking"]["benchmark_boost"], 0)
        self.assertFalse(one["historical_context"]["benchmark"]["recurring_weakness"])

        self._write_current_benchmark(failed=1)
        current = discover_opportunities(self.root, limit=20, now_epoch=2_000_000_002)
        after = next(row for row in current["opportunities"] if row["type"] == "complex_function")

        self.assertGreater(after["ranking"]["benchmark_boost"], 0)
        benchmark = after["historical_context"]["benchmark"]
        self.assertTrue(benchmark["current_code_match"])
        self.assertTrue(benchmark["recurring_weakness"])
        self.assertEqual(benchmark["current_failed_reports"], 2)
        self.assertEqual(benchmark["failed"], 1)
        self.assertEqual(benchmark["suite"], "retrieval")
        self.assertGreater(after["priority_score"], before["priority_score"])

    def test_provider_reliability_requires_recurrence_before_ranking_boost(self):
        from sira.memory import MemoryStore, Observation
        from sira.opportunity import discover_opportunities

        (self.root / "src" / "sira" / "retrieval.py").write_text(
            self._complex_retrieval_module(), encoding="utf-8"
        )
        store = MemoryStore(self.root)

        recurring_failure = Observation(
            "failure", "provider", "retrieval", "provider unavailable", "observed",
            provider="demo_provider", error_code="http_503", signature="retrieval:demo_provider:http_503",
        )
        success = Observation(
            "success", "success", "retrieval", "retrieval succeeded", "validated",
            provider="demo_provider", signature="retrieval:demo_provider:completed",
        )
        single_failure = Observation(
            "failure", "rate_limit", "retrieval", "single rate limit", "observed",
            provider="single_provider", error_code="http_429", signature="retrieval:single_provider:http_429",
        )
        for idx in range(2):
            store.upsert(
                recurring_failure, run_id=f"a{idx + 1}" * 16,
                artifact_name="result.json", artifact_sha256=f"{idx + 1:064x}", outcome_status="failed",
            )
        store.upsert(
            success, run_id="b1" * 16, artifact_name="result.json",
            artifact_sha256="3" * 64, outcome_status="completed",
        )
        store.upsert(
            single_failure, run_id="c1" * 16, artifact_name="result.json",
            artifact_sha256="4" * 64, outcome_status="failed",
        )

        result = discover_opportunities(self.root, limit=20, now_epoch=2_000_000_000)
        row = next(item for item in result["opportunities"] if item["type"] == "complex_function")
        provider = row["historical_context"]["provider_reliability"]

        self.assertGreater(row["ranking"]["provider_history_boost"], 0)
        self.assertEqual(provider["capability"], "retrieval")
        names = {item["provider"] for item in provider["recurring_unreliable_providers"]}
        self.assertIn("demo_provider", names)
        self.assertNotIn("single_provider", names)
        self.assertTrue(provider["minimum_recurrence_enforced"])

    def test_history_metrics_are_exposed_in_scan_report(self):
        from sira.opportunity import discover_opportunities

        (self.root / "src" / "sira" / "retrieval.py").write_text(
            self._complex_retrieval_module(), encoding="utf-8"
        )
        result = discover_opportunities(self.root, limit=20, now_epoch=2_000_000_000)

        self.assertEqual(result["policy_version"], 3)
        self.assertIn("history", result["scan"])
        self.assertIn("current_benchmark_reports", result["scan"]["history"])
        self.assertIn("provider_history_rows", result["scan"]["history"])
        self.assertIn("cross-signal", result["selection_policy"])


if __name__ == "__main__":
    unittest.main()
