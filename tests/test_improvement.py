import io
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
import sys
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.cli import main
from sira.memory import MemoryStore, Observation
from sira.improvement import ImprovementStore, plan_improvement, research_memory, run_experiment


class FakeRunner:
    def __init__(self, *, sequence=None, tests_ok=True, benchmark_ok=True):
        self.sequence = list(sequence or [])
        self.tests_ok = tests_ok
        self.benchmark_ok = benchmark_ok
        self.calls = []

    def evaluate(self, root: Path, suite: str | None):
        self.calls.append((Path(root), suite))
        if self.sequence:
            tests_ok, benchmark_ok = self.sequence.pop(0)
        else:
            tests_ok, benchmark_ok = self.tests_ok, self.benchmark_ok
        return {
            "tests": {"passed": tests_ok, "test_count": 12, "returncode": 0 if tests_ok else 1},
            "benchmark": ({"passed": benchmark_ok, "passed_cases": 4, "failed_cases": 0 if benchmark_ok else 1,
                           "returncode": 0 if benchmark_ok else 1} if suite else None),
            "overall_passed": tests_ok and (benchmark_ok if suite else True),
        }


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "runs").mkdir()
        (self.root / "src" / "sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "sira.py").write_text("print('fixture')\n", encoding="utf-8")
        (self.root / "src" / "sira" / "fixture.py").write_text("X = 1\n", encoding="utf-8")
        (self.root / "tests" / "test_fixture.py").write_text("# fixture\n", encoding="utf-8")
        (self.root / "benchmarks" / "cases.json").write_text("{}\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _add_memory(self, obs: Observation, run_id: str):
        store = MemoryStore(self.root)
        return store.upsert(obs, run_id=run_id, artifact_name="result.json",
                            artifact_sha256=(run_id[0] * 64), outcome_status="failed")[0]

    def test_plan_prioritizes_repeated_high_impact_failure_and_persists_artifact(self):
        repeated = Observation("failure", "rate_limit", "scholarly_research",
                               "Semantic Scholar rate limited", "observed",
                               provider="semantic_scholar", error_code="rate_limited",
                               signature="paper:rate")
        repeated_id = None
        for ch in ("a", "b", "c"):
            repeated_id = self._add_memory(repeated, ch * 32)
        one_off = Observation("failure", "unknown", "retrieval", "Unknown retrieval failure", "observed",
                              provider="tavily", error_code="unknown", signature="retrieval:unknown")
        self._add_memory(one_off, "d" * 32)

        plan = plan_improvement(self.root)
        self.assertEqual(plan["kind"], "improvement_plan")
        self.assertEqual(plan["target"]["memory_id"], repeated_id)
        self.assertGreater(plan["target"]["priority_score"], plan["alternatives"][0]["priority_score"])
        path = ImprovementStore(self.root).plan_path(plan["plan_id"])
        self.assertTrue(path.is_file())

    def test_plan_excludes_fixture_memories_from_autonomous_targeting(self):
        fixture_failure = Observation(
            "failure", "unknown", "reading", "Synthetic fixture extraction failed", "observed",
            provider="fixture_reader", error_code="extraction_failed", signature="fixture:extract",
        )
        for index in range(18):
            self._add_memory(fixture_failure, f"{index + 10:032x}")

        live_failure = Observation(
            "failure", "rate_limit", "scholarly_research", "Semantic Scholar rate limited", "observed",
            provider="semantic_scholar", error_code="rate_limited", signature="live:paper:rate",
        )
        live_id = self._add_memory(live_failure, "f" * 32)

        plan = plan_improvement(self.root)
        self.assertEqual(plan["target"]["memory_id"], live_id)
        self.assertTrue(all(not str(row.get("provider") or "").startswith("fixture")
                            for row in [plan["target"], *plan["alternatives"]]))
        self.assertIn("synthetic", plan["selection_policy"])

    def test_research_excludes_synthetic_related_memories_from_context_and_success_count(self):
        target = Observation(
            "failure", "rate_limit", "scholarly_research", "Semantic Scholar rate limited", "observed",
            provider="semantic_scholar", error_code="rate_limited", signature="live:paper:rate:research",
        )
        target_id = self._add_memory(target, "a" * 32)

        synthetic_success = Observation(
            "success", "success", "scholarly_research",
            "scholarly research completed successfully via paper_fixture", "validated",
            provider="paper_fixture", signature="synthetic:paper:success",
        )
        self._add_memory(synthetic_success, "b" * 32)
        synthetic_failure = Observation(
            "failure", "bad_source", "scholarly_research",
            "paper_fixture no results failure in scholarly_research", "observed",
            provider="paper_fixture", error_code="no_results", signature="synthetic:paper:failure",
        )
        self._add_memory(synthetic_failure, "c" * 32)
        real_success = Observation(
            "success", "success", "scholarly_research",
            "scholarly research completed successfully via arxiv", "validated",
            provider="arxiv", signature="live:arxiv:success",
        )
        real_success_id = self._add_memory(real_success, "d" * 32)

        hypothesis = research_memory(self.root, target_id)
        related_ids = {row["memory_id"] for row in hypothesis["related_memories"]}
        providers = {row.get("provider") for row in hypothesis["related_memories"]}

        self.assertIn(real_success_id, related_ids)
        self.assertNotIn("paper_fixture", providers)
        self.assertEqual(hypothesis["validated_successes_found"], 1)
        self.assertIn("synthetic", hypothesis["research_context_policy"])

    def test_research_policy_change_does_not_reuse_stale_pre_hygiene_hypothesis(self):
        target = Observation(
            "failure", "rate_limit", "scholarly_research", "Semantic Scholar rate limited", "observed",
            provider="semantic_scholar", error_code="rate_limited", signature="live:paper:rate:stale",
        )
        target_id = self._add_memory(target, "e" * 32)
        memory = MemoryStore(self.root).show(target_id)
        statement = (
            "For scholarly_research, Provider throttling may be mitigated by bounded retry, cache, and "
            "independent fallback providers. Verify the current fallback/cache path against the paper "
            "benchmark before proposing more retry pressure."
        )
        old_payload = {
            "memory_id": memory["memory_id"],
            "category": memory["category"],
            "capability": memory["capability"],
            "statement": " ".join(statement.casefold().split()),
            "suite": "papers",
        }
        old_fingerprint = hashlib.sha256(
            json.dumps(old_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        stale_id = "ih_" + ("1" * 32)
        ImprovementStore(self.root).save_hypothesis({
            "schema_version": 1,
            "sira_version": "0.9.1",
            "kind": "improvement_hypothesis",
            "hypothesis_id": stale_id,
            "fingerprint": old_fingerprint,
            "created_at": "2026-09-17T00:00:00+00:00",
            "memory_id": target_id,
            "statement": statement,
            "related_memories": [{"provider": "paper_fixture"}],
            "validated_successes_found": 99,
            "experiment_type": "regression_check",
            "benchmark_suite": "papers",
        })

        fresh = research_memory(self.root, target_id)
        self.assertNotEqual(fresh["hypothesis_id"], stale_id)
        self.assertEqual(fresh["research_context_policy_version"], 2)
        self.assertEqual(fresh["validated_successes_found"], 0)

    def test_genuine_benchmark_regression_remains_autonomous_target_eligible(self):
        regression = Observation(
            "failure", "benchmark_regression", "benchmarking", "Reading benchmark regression", "observed",
            provider="sira-reading-v1", error_code="benchmark_regression", signature="benchmark:reading:failed",
        )
        regression_id = self._add_memory(regression, "e" * 32)
        plan = plan_improvement(self.root)
        self.assertEqual(plan["target"]["memory_id"], regression_id)

    def test_research_transitions_memory_and_reuses_identical_hypothesis(self):
        obs = Observation("failure", "citation", "synthesis", "Unknown citation rejection", "observed",
                          provider="gemini", error_code="unknown_citation", signature="citation:unknown")
        memory_id = self._add_memory(obs, "e" * 32)
        first = research_memory(self.root, memory_id)
        second = research_memory(self.root, memory_id)

        self.assertEqual(first["kind"], "improvement_hypothesis")
        self.assertEqual(first["hypothesis_id"], second["hypothesis_id"])
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        detail = MemoryStore(self.root).show(memory_id)
        self.assertEqual(detail["status"], "researched")
        self.assertEqual([x["to_status"] for x in detail["transitions"]], ["researched"])

    def test_regression_check_experiment_is_isolated_and_validates_historical_failure(self):
        obs = Observation("failure", "rate_limit", "scholarly_research", "Rate limited provider", "observed",
                          provider="semantic_scholar", error_code="rate_limited", signature="paper:rate")
        memory_id = self._add_memory(obs, "f" * 32)
        hypothesis = research_memory(self.root, memory_id)
        self.assertEqual(hypothesis["experiment_type"], "regression_check")

        runner = FakeRunner(tests_ok=True, benchmark_ok=True)
        result = run_experiment(self.root, hypothesis["hypothesis_id"], runner=runner)
        self.assertEqual(result["outcome"], "validated_existing_mitigation")
        self.assertTrue(result["candidate"]["overall_passed"])
        self.assertEqual(MemoryStore(self.root).show(memory_id)["status"], "validated")
        workspace = Path(result["workspace"])
        self.assertTrue((workspace / "candidate" / "src" / "sira" / "fixture.py").is_file())
        self.assertFalse((workspace / "candidate" / "memory").exists())
        self.assertFalse((workspace / "candidate" / "runs").exists())
        self.assertTrue((workspace / "candidate_manifest.json").is_file())
        self.assertEqual(result["candidate_policy"]["policy_version"], 1)
        self.assertFalse(result["candidate_policy"]["promotion_allowed"])
        self.assertFalse(result["candidate_policy"]["credentials_copied"])

    def test_experiment_persists_evaluator2_report_before_any_promotion(self):
        obs = Observation("failure", "rate_limit", "scholarly_research", "Rate limited provider", "observed",
                          provider="semantic_scholar", error_code="rate_limited", signature="paper:evaluator2")
        memory_id = self._add_memory(obs, "9" * 32)
        hypothesis = research_memory(self.root, memory_id)

        result = run_experiment(self.root, hypothesis["hypothesis_id"], runner=FakeRunner())

        self.assertEqual(result["evaluator2"]["kind"], "evaluator2_report")
        self.assertEqual(result["evaluator2"]["decision"], "accept")
        self.assertEqual(result["evaluator2"]["decision_code"], "no_change_verified")
        self.assertFalse(result["evaluator2"]["promotion_recommended"])
        self.assertFalse(result["promotion_performed"])
        saved = ImprovementStore(self.root).latest_experiment_for_hypothesis(hypothesis["hypothesis_id"])
        self.assertEqual(saved["evaluator2"]["policy_version"], 1)

    def test_failed_candidate_is_rejected_without_touching_main_source(self):
        obs = Observation("failure", "code_test", "retrieval", "Regression in tests", "observed",
                          error_code="test_failure", signature="code:test")
        memory_id = self._add_memory(obs, "1" * 32)
        hypothesis = research_memory(self.root, memory_id)
        runner = FakeRunner(sequence=[(True, True), (False, False)])
        before = (self.root / "src" / "sira" / "fixture.py").read_text(encoding="utf-8")
        result = run_experiment(self.root, hypothesis["hypothesis_id"], runner=runner)
        self.assertEqual(result["outcome"], "rejected_regression")
        self.assertEqual(MemoryStore(self.root).show(memory_id)["status"], "rejected")
        self.assertEqual((self.root / "src" / "sira" / "fixture.py").read_text(encoding="utf-8"), before)


    def test_rejected_hypothesis_is_not_repeated_without_a_new_strategy(self):
        obs = Observation("failure", "code_test", "retrieval", "Persistent regression", "observed",
                          error_code="test_failure", signature="code:persistent")
        memory_id = self._add_memory(obs, "2" * 32)
        hypothesis = research_memory(self.root, memory_id)
        run_experiment(self.root, hypothesis["hypothesis_id"],
                       runner=FakeRunner(sequence=[(True, True), (False, False)]))
        blocked = research_memory(self.root, memory_id)
        self.assertTrue(blocked["reused"])
        self.assertTrue(blocked["repeat_blocked"])
        self.assertEqual(MemoryStore(self.root).show(memory_id)["status"], "rejected")
        with self.assertRaisesRegex(ValueError, "Rejected hypothesis cannot be repeated"):
            run_experiment(self.root, hypothesis["hypothesis_id"], runner=FakeRunner())

    def test_ids_are_validated_and_status_is_json_serializable(self):
        with self.assertRaises(ValueError):
            ImprovementStore(self.root).load_hypothesis("../escape")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--root", str(self.root), "improve", "status"]), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["plans"], 0)


    def test_research_uses_ranked_autonomous_retrieval_and_records_usage_feedback(self):
        from unittest.mock import patch

        store = MemoryStore(self.root)
        target_id, _ = store.upsert(
            Observation(
                "failure", "network", "retrieval",
                "target retrieval timeout", "observed",
                provider="tavily", error_code="timeout",
                signature="consumer-integration-target",
            ),
            run_id="91" * 16, artifact_name="result.json",
            artifact_sha256="1" * 64, outcome_status="failed",
        )
        success_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "validated retrieval recovery", "validated",
                provider="tavily",
                signature="consumer-integration-success",
            ),
            run_id="92" * 16, artifact_name="result.json",
            artifact_sha256="2" * 64, outcome_status="completed",
        )

        calls = []

        def fake_retrieve(instance, query, limit=20, *, autonomous=False):
            calls.append((query, limit, autonomous))
            row = instance.show(success_id)
            row["retrieval_score"] = 0.91
            row["score_reasons"] = [
                {"component": "relevance", "effect": "boost", "value": 0.45},
                {"component": "confidence", "effect": "boost", "value": 0.30},
                {"component": "freshness", "effect": "boost", "value": 0.08},
            ]
            return [row]

        with patch.object(MemoryStore, "retrieve", new=fake_retrieve), \
             patch.object(
                 MemoryStore,
                 "search",
                 side_effect=AssertionError(
                     "research_memory must not use legacy search() for autonomous context"
                 ),
             ):
            hypothesis = research_memory(self.root, target_id)

        self.assertEqual(calls, [("retrieval", 20, True)])
        self.assertEqual(len(hypothesis["related_memories"]), 1)
        related = hypothesis["related_memories"][0]
        self.assertEqual(related["memory_id"], success_id)
        self.assertEqual(related["retrieval_score"], 0.91)
        self.assertTrue(related["score_reasons"])

        outcomes = MemoryStore(self.root).outcomes(success_id)
        used = [
            row for row in outcomes
            if row["outcome_type"] == "retrieval_used"
            and row["context"].get("consumer") == "improvement.research_memory"
        ]
        self.assertEqual(len(used), 1)
        self.assertEqual(used[0]["context"]["target_memory_id"], target_id)


if __name__ == "__main__":
    unittest.main()
