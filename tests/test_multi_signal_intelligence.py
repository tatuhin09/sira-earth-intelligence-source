import json
from pathlib import Path
import shutil
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class MultiSignalOpportunityTests(unittest.TestCase):
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
    def _multi_signal_module():
        return '''\
def complex_path(value):
    total = value
    if total == 0:
        total += 1
    if total == 1:
        total += 1
    if total == 2:
        total += 1
    if total == 3:
        total += 1
    if total == 4:
        total += 1
    if total == 5:
        total += 1
    if total == 6:
        total += 1
    if total == 7:
        total += 1
    if total == 8:
        total += 1
    if total == 9:
        total += 1
    if total == 10:
        total += 1
    if total == 11:
        total += 1
    if total == 12:
        total += 1
    total += 13
    total += 14
    total += 15
    total += 16
    total += 17
    total += 18
    total += 19
    total += 20
    total += 21
    total += 22
    total += 23
    total += 24
    total += 25
    total += 26
    total += 27
    total += 28
    return total


def uncovered_api(value):
    total = value
    if total < 0:
        total = -total
    if total == 0:
        total = 1
    total += 1
    total += 2
    total += 3
    total += 4
    total += 5
    total += 6
    total += 7
    total += 8
    total += 9
    total += 10
    return total


def swallow_error(value):
    try:
        return int(value)
    except Exception:
        pass
    return None


def duplicate_a(value):
    first = value + 1
    second = first * 2
    third = second - 3
    return third


def duplicate_b(value):
    first = value + 1
    second = first * 2
    third = second - 3
    return third
'''

    def test_discovery_emits_complexity_missing_test_weak_error_and_duplication_signals(self):
        from sira.opportunity import discover_opportunities

        (self.root / "src" / "sira" / "retrieval.py").write_text(self._multi_signal_module(), encoding="utf-8")
        result = discover_opportunities(self.root, limit=20, now_epoch=2_000_000_000)
        kinds = {row["type"] for row in result["opportunities"]}

        self.assertIn("complex_function", kinds)
        self.assertIn("missing_test_reference", kinds)
        self.assertIn("weak_error_handling", kinds)
        self.assertIn("duplicate_function_body", kinds)
        self.assertEqual(result["api_requests"], 0)
        self.assertIn("detector_signal_counts", result["scan"])
        self.assertGreaterEqual(result["scan"]["detector_signal_counts"]["missing_test_reference"], 1)

    def test_direct_test_reference_suppresses_missing_test_signal(self):
        from sira.opportunity import discover_opportunities

        (self.root / "src" / "sira" / "retrieval.py").write_text(self._multi_signal_module(), encoding="utf-8")
        (self.root / "tests" / "test_retrieval.py").write_text(
            "from sira.retrieval import uncovered_api\n\ndef test_api():\n    assert uncovered_api(1) > 0\n",
            encoding="utf-8",
        )
        result = discover_opportunities(self.root, limit=20, now_epoch=2_000_000_000)

        missing = [row for row in result["opportunities"] if row["type"] == "missing_test_reference"]
        self.assertFalse(any(row.get("symbol") == "uncovered_api" for row in missing))

    def test_missing_test_signal_can_be_research_ready_without_existing_test_reference(self):
        from sira.opportunity import discover_opportunities
        from sira.opportunity_evidence import build_opportunity_evidence

        module = self._multi_signal_module()
        (self.root / "src" / "sira" / "retrieval.py").write_text(module, encoding="utf-8")
        (self.root / "src" / "sira" / "consumer.py").write_text(
            "from .retrieval import uncovered_api\n\ndef use_it(value):\n    return uncovered_api(value)\n",
            encoding="utf-8",
        )
        result = discover_opportunities(self.root, limit=20, now_epoch=2_000_000_000)
        opportunity = next(
            row for row in result["opportunities"]
            if row["type"] == "missing_test_reference" and row.get("symbol") == "uncovered_api"
        )

        evidence = build_opportunity_evidence(self.root, opportunity["opportunity_id"])

        self.assertEqual(evidence["assessment"]["decision"], "research_ready")
        self.assertFalse(evidence["tests"])
        goal = evidence["success_criteria"]["structural_goal"]
        self.assertEqual(goal["metric"], "test_calls")
        self.assertEqual(goal["baseline"], 0)
        self.assertEqual(goal["target_min"], 1)

    def test_research_queries_and_relevance_are_signal_specific(self):
        from sira.opportunity_research import _research_query, _relevance, _strategies

        fixtures = {
            "missing_test_reference": ("test coverage", "regression testing"),
            "weak_error_handling": ("exception handling", "error handling"),
            "duplicate_function_body": ("code duplication", "duplicate code"),
        }
        for kind, (query_term, title) in fixtures.items():
            evidence = {
                "opportunity": {"type": kind},
                "success_criteria": {"structural_goal": {"metric": "static_signal"}},
            }
            query = _research_query(evidence)
            self.assertIn(query_term, query)
            relevance = _relevance({"title": title, "abstract_excerpt": ""}, evidence)
            self.assertTrue(relevance["eligible"])
            self.assertTrue(_strategies(evidence, ["R1"]))


class MultiSignalGoalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.main = Path(self.tmp.name) / "main"
        self.candidate = Path(self.tmp.name) / "candidate"
        for root in (self.main, self.candidate):
            (root / "src" / "sira").mkdir(parents=True)
            (root / "tests").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _hypothesis(self, metric, baseline, *, target_max=None, target_min=None, symbol="target"):
        goal = {"metric": metric, "baseline": baseline}
        if target_max is not None:
            goal["target_max"] = target_max
        if target_min is not None:
            goal["target_min"] = target_min
        return {
            "target": {"path": "src/sira/retrieval.py", "symbol": symbol},
            "success_criteria": {"structural_goal": goal},
        }

    def test_trusted_goal_checker_is_independent_from_modifiable_detectors(self):
        source = (ROOT / "src" / "sira" / "autonomous_promotion.py").read_text(encoding="utf-8")
        self.assertNotIn("opportunity_detectors", source)

    def test_test_reference_goal_requires_candidate_reference(self):
        from sira.autonomous_promotion import _structural_goal_check

        source = "def target(value):\n    return value + 1\n"
        for root in (self.main, self.candidate):
            (root / "src" / "sira" / "retrieval.py").write_text(source, encoding="utf-8")
        (self.candidate / "tests" / "test_retrieval.py").write_text(
            "from sira.retrieval import target\n\ndef test_target():\n    assert target(1) == 2\n",
            encoding="utf-8",
        )

        result = _structural_goal_check(
            self.main, self.candidate,
            self._hypothesis("test_calls", 0, target_min=1),
        )
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(result["baseline"], 0)
        self.assertGreaterEqual(result["candidate"], 1)

    def test_test_call_goal_ignores_unrelated_same_named_function(self):
        from sira.autonomous_promotion import _structural_goal_check

        source = "def target(value):\n    return value + 1\n"
        for root in (self.main, self.candidate):
            (root / "src" / "sira" / "retrieval.py").write_text(source, encoding="utf-8")
        (self.candidate / "tests" / "test_other.py").write_text(
            "def target(value):\n    return value\n\ndef test_other():\n    assert target(1) == 1\n",
            encoding="utf-8",
        )

        result = _structural_goal_check(
            self.main, self.candidate,
            self._hypothesis("test_calls", 0, target_min=1),
        )
        self.assertEqual(result["decision"], "fail")
        self.assertEqual(result["candidate"], 0)

    def test_weak_exception_goal_counts_silent_broad_handlers(self):
        from sira.autonomous_promotion import _structural_goal_check

        (self.main / "src" / "sira" / "retrieval.py").write_text(
            "def target(value):\n    try:\n        return int(value)\n    except Exception:\n        pass\n    return None\n",
            encoding="utf-8",
        )
        (self.candidate / "src" / "sira" / "retrieval.py").write_text(
            "def target(value):\n    try:\n        return int(value)\n    except (TypeError, ValueError):\n        return None\n",
            encoding="utf-8",
        )

        result = _structural_goal_check(
            self.main, self.candidate,
            self._hypothesis("weak_exception_handlers", 1, target_max=0),
        )
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(result["candidate"], 0)

    def test_duplicate_body_goal_requires_duplicate_to_be_removed(self):
        from sira.autonomous_promotion import _structural_goal_check

        main_source = '''\
def target(value):
    first = value + 1
    second = first * 2
    third = second - 3
    return third


def peer(value):
    first = value + 1
    second = first * 2
    third = second - 3
    return third
'''
        candidate_source = '''\
def target(value):
    first = value + 1
    second = first * 2
    third = second - 3
    return third


def peer(value):
    return (value + 1) * 2 - 3
'''
        (self.main / "src" / "sira" / "retrieval.py").write_text(main_source, encoding="utf-8")
        (self.candidate / "src" / "sira" / "retrieval.py").write_text(candidate_source, encoding="utf-8")

        result = _structural_goal_check(
            self.main, self.candidate,
            self._hypothesis("duplicate_body_matches", 1, target_max=0),
        )
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(result["candidate"], 0)

    def test_writer_hypothesis_renders_minimum_goal_direction(self):
        from sira.opportunity_handoff import _writer_hypothesis

        research = {
            "research_id": "or_" + "1" * 32,
            "opportunity_id": "op_" + "2" * 32,
            "target": {
                "path": "src/sira/retrieval.py",
                "symbol": "target",
                "benchmark_suite": "retrieval",
                "structural_goal": {"metric": "test_calls", "baseline": 0, "target_min": 1},
            },
            "citations": [],
            "candidate_strategies": [],
        }
        evidence = {"opportunity": {"summary": "missing direct test"}, "related_memories": []}

        hypothesis = _writer_hypothesis(research, evidence)
        self.assertIn(">= 1", hypothesis["statement"])
        self.assertEqual(hypothesis["success_criteria"]["structural_goal"]["target_min"], 1)


if __name__ == "__main__":
    unittest.main()
