from pathlib import Path
import json
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _Writer:
    def __init__(self, edits):
        self.edits = edits
        self.last_report = {
            "model": {"provider": "fixture_code_model", "id": "fixture-v1"},
            "api_requests": 1,
            "input_tokens": 100,
            "output_tokens": 50,
            "context_sha256": "a" * 64,
        }
        self.context = None

    def propose_text_edits(self, context):
        self.context = dict(context)
        return dict(self.edits)


class _Runner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def evaluate(self, root, suite):
        self.calls.append((Path(root).resolve(), suite))
        if not self.responses:
            raise AssertionError("unexpected runner call")
        return self.responses.pop(0)


def _health(tests=204, cases=4, passed=True):
    return {
        "tests": {"passed": passed, "test_count": tests, "returncode": 0 if passed else 1},
        "benchmark": {
            "passed": passed,
            "passed_cases": cases if passed else 0,
            "failed_cases": 0 if passed else 1,
            "returncode": 0 if passed else 1,
        },
        "overall_passed": passed,
    }


class OpportunityWriterHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "main"
        (self.root / "src" / "sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        source = (
            "def target(value):\n"
            "    if value > 0:\n"
            "        if value % 2:\n"
            "            return 1\n"
            "        return 2\n"
            "    if value == 0:\n"
            "        return 0\n"
            "    return -1\n"
        )
        (self.root / "src" / "sira" / "feature.py").write_text(source, encoding="utf-8")
        for name in (
            "runtime.py", "cli.py", "self_modification.py", "evaluator1.py",
            "promotion.py", "rollback.py", "autonomous_promotion.py",
            "owner_identity.py", "audit_integrity.py", "safety_boundary.py",
        ):
            (self.root / "src" / "sira" / name).write_text(f"PROTECTED = {name!r}\n", encoding="utf-8")
        (self.root / "tests" / "test_feature.py").write_text("# characterization tests\n", encoding="utf-8")
        (self.root / "benchmarks" / "paper_reading_cases.json").write_text("{}\n", encoding="utf-8")
        (self.root / "docs" / "note.md").write_text("note\n", encoding="utf-8")
        (self.root / "sira.py").write_text("print('sira')\n", encoding="utf-8")
        (self.root / "README.md").write_text("readme\n", encoding="utf-8")
        (self.root / ".env.example").write_text("SAFE=1\n", encoding="utf-8")

        payload = (self.root / "src" / "sira" / "feature.py").read_bytes()
        import hashlib
        source_sha = hashlib.sha256(payload).hexdigest()
        self.evidence_id = "oe_" + "1" * 32
        self.research_id = "or_" + "2" * 32
        evidence = {
            "schema_version": 1,
            "kind": "opportunity_evidence_brief",
            "policy_version": 1,
            "evidence_id": self.evidence_id,
            "opportunity": {
                "opportunity_id": "op_" + "3" * 32,
                "fingerprint": "4" * 64,
                "type": "complex_function",
                "path": "src/sira/feature.py",
                "symbol": "target",
                "source_sha256": source_sha,
                "evidence": {"line": 1, "end_line": 8, "loc": 8, "branch_points": 3},
            },
            "assessment": {"decision": "research_ready"},
            "success_criteria": {
                "benchmark_suite": "paper-reading",
                "structural_goal": {"metric": "branch_points", "baseline": 3, "target_max": 2},
            },
        }
        research = {
            "schema_version": 1,
            "kind": "opportunity_free_research_brief",
            "policy_version": 3,
            "research_id": self.research_id,
            "evidence_id": self.evidence_id,
            "opportunity_id": evidence["opportunity"]["opportunity_id"],
            "target": {
                "path": "src/sira/feature.py",
                "symbol": "target",
                "structural_goal": {"metric": "branch_points", "baseline": 3, "target_max": 2},
                "benchmark_suite": "paper-reading",
            },
            "citations": [
                {"citation_id": "R1", "provider": "crossref", "title": "Refactoring maintainability", "url": "https://doi.org/10.1/x", "abstract_excerpt": None},
                {"citation_id": "R2", "provider": "openalex", "title": "Behavior preserving refactoring", "url": "https://openalex.org/W1", "abstract_excerpt": "Extract method can reduce complexity while preserving behavior."},
            ],
            "candidate_strategies": [
                {"strategy": "Extract cohesive phases while preserving behavior.", "citation_ids": ["R1", "R2"]}
            ],
            "research_quality": {"decision": "writer_ready"},
            "writer_handoff_allowed": True,
            "paid_spending": False,
            "metered_model_requests": 0,
        }
        evidence_dir = self.root / "improvements" / "opportunities" / "evidence"
        research_dir = self.root / "improvements" / "opportunities" / "research"
        evidence_dir.mkdir(parents=True)
        research_dir.mkdir(parents=True)
        (evidence_dir / f"{self.evidence_id}.json").write_text(json.dumps(evidence), encoding="utf-8")
        (research_dir / f"{self.research_id}.json").write_text(json.dumps(research), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_handoff_builds_research_grounded_writer_context_and_promotes_when_goal_met(self):
        from sira.opportunity_handoff import run_opportunity_writer_handoff

        writer = _Writer({
            "src/sira/feature.py": (
                "def target(value):\n"
                "    if value > 0:\n"
                "        return 2 - (value % 2)\n"
                "    return 0 if value == 0 else -1\n"
            )
        })
        runner = _Runner([_health(), _health(tests=205), _health(tests=205)])
        result = run_opportunity_writer_handoff(self.root, self.research_id, writer=writer, runner=runner)

        self.assertEqual(result["status"], "promoted")
        self.assertEqual(result["structural_check"]["decision"], "pass")
        self.assertLessEqual(result["structural_check"]["candidate"], 2)
        self.assertTrue(result["promotion_performed"])
        self.assertEqual(result["research_id"], self.research_id)
        self.assertIn("R2", json.dumps(writer.context))
        self.assertEqual(writer.context["target"]["path"], "src/sira/feature.py")
        self.assertEqual(runner.calls[0][1], "paper-reading")

    def test_structural_goal_failure_rejects_before_tests_or_evaluators(self):
        from sira.opportunity_handoff import run_opportunity_writer_handoff

        writer = _Writer({
            "src/sira/feature.py": (
                "def target(value):\n"
                "    if value > 0:\n"
                "        if value % 2:\n"
                "            return 1\n"
                "        return 2\n"
                "    if value == 0:\n"
                "        return 0\n"
                "    return -1\n"
            )
        })
        runner = _Runner([])
        result = run_opportunity_writer_handoff(self.root, self.research_id, writer=writer, runner=runner)

        self.assertEqual(result["status"], "rejected_structural_goal")
        self.assertFalse(result["promotion_performed"])
        self.assertEqual(result["structural_check"]["decision"], "fail")
        self.assertEqual(runner.calls, [])

    def test_not_writer_ready_research_never_calls_writer(self):
        from sira.opportunity_handoff import run_opportunity_writer_handoff

        path = self.root / "improvements" / "opportunities" / "research" / f"{self.research_id}.json"
        research = json.loads(path.read_text(encoding="utf-8"))
        research["writer_handoff_allowed"] = False
        research["research_quality"] = {"decision": "insufficient_external_evidence"}
        path.write_text(json.dumps(research), encoding="utf-8")
        writer = _Writer({"src/sira/feature.py": "VALUE = 2\n"})

        with self.assertRaisesRegex(ValueError, "not writer-ready"):
            run_opportunity_writer_handoff(self.root, self.research_id, writer=writer, runner=_Runner([]))
        self.assertIsNone(writer.context)

    def test_stale_target_is_rejected_before_writer(self):
        from sira.opportunity_handoff import run_opportunity_writer_handoff

        (self.root / "src" / "sira" / "feature.py").write_text("def target(value):\n    return value\n", encoding="utf-8")
        writer = _Writer({"src/sira/feature.py": "def target(value):\n    return 1\n"})
        with self.assertRaisesRegex(ValueError, "stale"):
            run_opportunity_writer_handoff(self.root, self.research_id, writer=writer, runner=_Runner([]))
        self.assertIsNone(writer.context)


if __name__ == "__main__":
    unittest.main()
