from pathlib import Path
import json
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


NOW = 2_000_000_000.0


def _memory(memory_id: str, *, category: str, kind: str = "failure", score: int = 10):
    return {
        "memory_id": memory_id,
        "kind": kind,
        "status": "observed" if kind == "failure" else "rejected",
        "category": category,
        "capability": "retrieval",
        "provider": "real_provider",
        "occurrence_count": 1,
        "priority_score": score,
        "last_seen_at": "2030-01-01T00:00:00+00:00",
    }


def _opportunity(*, fingerprint: str, path: str, symbol: str, score: int):
    return {
        "opportunity_id": "op_" + fingerprint[:32],
        "fingerprint": fingerprint,
        "type": "complex_function",
        "path": path,
        "symbol": symbol,
        "summary": f"{path}:{symbol}",
        "priority_score": score,
        "source_sha256": "f" * 64,
        "evidence": {"branch_points": 30, "loc": 100},
        "cooldown": {"eligible": True, "last_attempt_at": None, "remaining_seconds": 0},
    }


def _discovery(opportunities):
    return {
        "schema_version": 1,
        "kind": "opportunity_discovery",
        "opportunities": list(opportunities),
        "eligible_count": len(opportunities),
        "suppressed_cooldown_count": 0,
        "api_requests": 0,
        "paid_spending": False,
    }


class AutonomousTargetingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src/sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        for module, symbol in (
            ("retrieval", "load_evidence_run"),
            ("synthesis", "answer_run"),
            ("paper_reading", "paper_read_run"),
        ):
            (self.root / "src/sira" / f"{module}.py").write_text(
                f"def {symbol}():\n    return 1\n", encoding="utf-8",
            )
            (self.root / "tests" / f"test_{module}.py").write_text(
                f"from sira.{module} import {symbol}\n", encoding="utf-8",
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_benchmark_regression_memory_outranks_higher_scored_opportunity(self):
        from sira.autonomous_targeting import select_autonomous_target

        memory = _memory("m_" + "1" * 32, category="benchmark_regression", score=22)
        opportunity = _opportunity(
            fingerprint="2" * 64,
            path="src/sira/retrieval.py",
            symbol="load_evidence_run",
            score=900,
        )
        result = select_autonomous_target(
            self.root,
            now_epoch=NOW,
            memory_candidates_fn=lambda root, limit: [memory],
            opportunity_discovery_fn=lambda root, limit, now_epoch: _discovery([opportunity]),
        )

        self.assertEqual(result["target"]["target_kind"], "memory")
        self.assertEqual(result["target"]["priority_class"], 1)
        self.assertEqual(
            result["target"]["benchmark_evidence"]["classification"],
            "no_current_evidence",
        )
        self.assertEqual(result["target"]["memory_id"], memory["memory_id"])
        self.assertEqual(result["api_requests"], 0)
        self.assertFalse(result["paid_spending"])

    def test_other_unresolved_memory_still_outranks_proactive_opportunity(self):
        from sira.autonomous_targeting import select_autonomous_target

        memory = _memory("m_" + "3" * 32, category="provider", score=14)
        opportunity = _opportunity(
            fingerprint="4" * 64,
            path="src/sira/synthesis.py",
            symbol="answer_run",
            score=700,
        )
        result = select_autonomous_target(
            self.root,
            now_epoch=NOW,
            memory_candidates_fn=lambda root, limit: [memory],
            opportunity_discovery_fn=lambda root, limit, now_epoch: _discovery([opportunity]),
        )

        self.assertEqual(result["target"]["target_kind"], "memory")
        self.assertEqual(result["target"]["priority_class"], 1)

    def test_opportunity_is_selected_when_no_memory_target_exists(self):
        from sira.autonomous_targeting import select_autonomous_target

        opportunity = _opportunity(
            fingerprint="5" * 64,
            path="src/sira/retrieval.py",
            symbol="load_evidence_run",
            score=401,
        )
        result = select_autonomous_target(
            self.root,
            now_epoch=NOW,
            memory_candidates_fn=lambda root, limit: [],
            opportunity_discovery_fn=lambda root, limit, now_epoch: _discovery([opportunity]),
        )

        self.assertEqual(result["target"]["target_kind"], "opportunity")
        self.assertEqual(result["target"]["priority_class"], 3)
        self.assertEqual(result["target"]["opportunity_id"], opportunity["opportunity_id"])
        self.assertEqual(result["target"]["effective_priority_score"], 401)

    def test_recent_successful_lineage_suppresses_changed_fingerprint(self):
        from sira.autonomous_targeting import OpportunityLineageStore, select_autonomous_target

        promoted = _opportunity(
            fingerprint="6" * 64,
            path="src/sira/paper_reading.py",
            symbol="paper_read_run",
            score=428,
        )
        changed = dict(promoted)
        changed["fingerprint"] = "7" * 64
        changed["opportunity_id"] = "op_" + changed["fingerprint"][:32]
        changed["source_sha256"] = "8" * 64
        other = _opportunity(
            fingerprint="9" * 64,
            path="src/sira/retrieval.py",
            symbol="load_evidence_run",
            score=401,
        )
        OpportunityLineageStore(self.root).record_promotion(
            promoted,
            outcome="promotion_committed",
            attempted_at_epoch=NOW - 3600,
        )

        result = select_autonomous_target(
            self.root,
            now_epoch=NOW,
            memory_candidates_fn=lambda root, limit: [],
            opportunity_discovery_fn=lambda root, limit, now_epoch: _discovery([changed, other]),
        )

        self.assertEqual(result["target"]["opportunity_id"], other["opportunity_id"])
        self.assertEqual(result["suppressed_lineage_count"], 1)
        suppressed = result["suppressed_lineage"][0]
        self.assertEqual(suppressed["path"], "src/sira/paper_reading.py")
        self.assertGreater(suppressed["lineage"]["remaining_seconds"], 0)

    def test_older_successful_lineage_applies_diminishing_return_penalty(self):
        from sira.autonomous_targeting import OpportunityLineageStore, select_autonomous_target

        opportunity = _opportunity(
            fingerprint="a" * 64,
            path="src/sira/paper_reading.py",
            symbol="paper_read_run",
            score=428,
        )
        OpportunityLineageStore(self.root).record_promotion(
            opportunity,
            outcome="promotion_committed",
            attempted_at_epoch=NOW - (2 * 24 * 60 * 60),
        )
        changed = dict(opportunity)
        changed["fingerprint"] = "b" * 64
        changed["opportunity_id"] = "op_" + changed["fingerprint"][:32]

        result = select_autonomous_target(
            self.root,
            now_epoch=NOW,
            memory_candidates_fn=lambda root, limit: [],
            opportunity_discovery_fn=lambda root, limit, now_epoch: _discovery([changed]),
        )

        self.assertEqual(result["target"]["opportunity_id"], changed["opportunity_id"])
        self.assertGreater(result["target"]["lineage"]["priority_penalty"], 0)
        self.assertLess(result["target"]["effective_priority_score"], changed["priority_score"])
        self.assertTrue(result["target"]["lineage"]["eligible"])

    def test_existing_handoff_artifact_is_imported_into_lineage(self):
        from sira.autonomous_targeting import select_autonomous_target

        handoffs = self.root / "improvements" / "opportunities" / "handoffs"
        handoffs.mkdir(parents=True)
        handoff = {
            "schema_version": 1,
            "kind": "opportunity_writer_handoff",
            "status": "promoted",
            "outcome": "promotion_committed",
            "promotion_performed": True,
            "cooldown": {
                "opportunity_id": "op_" + "c" * 32,
                "type": "complex_function",
                "path": "src/sira/paper_reading.py",
                "symbol": "paper_read_run",
                "outcome": "promotion_committed",
                "attempted_at_epoch": NOW - 60,
                "attempted_at": "2033-05-18T03:32:20+00:00",
            },
        }
        (handoffs / ("ohf_" + "d" * 32 + ".json")).write_text(json.dumps(handoff), encoding="utf-8")
        changed = _opportunity(
            fingerprint="e" * 64,
            path="src/sira/paper_reading.py",
            symbol="paper_read_run",
            score=428,
        )

        result = select_autonomous_target(
            self.root,
            now_epoch=NOW,
            memory_candidates_fn=lambda root, limit: [],
            opportunity_discovery_fn=lambda root, limit, now_epoch: _discovery([changed]),
        )

        self.assertIsNone(result["target"])
        self.assertEqual(result["status"], "idle_no_candidate")
        self.assertEqual(result["lineage_imported_promotions"], 1)
        self.assertEqual(result["suppressed_lineage_count"], 1)


if __name__ == "__main__":
    unittest.main()
