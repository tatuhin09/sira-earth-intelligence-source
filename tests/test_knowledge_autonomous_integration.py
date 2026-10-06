import os
from pathlib import Path
import sys, tempfile, unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.access_runtime import target_identity
from sira.runtime import RuntimeStateStore


def knowledge_target():
    return {
        "target_kind": "knowledge_revalidation",
        "priority_class": 2,
        "priority_class_name": "knowledge_revalidation",
        "knowledge_key": "fact.stale",
        "claim_text": "A stale verified claim needs revalidation.",
        "effective_priority_score": 1000,
        "freshness": "stale",
    }


class KnowledgeAutonomousIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tests").mkdir()
        (self.root / "tests/test_retrieval.py").write_text(
            "from sira.retrieval import load_evidence_run\n", encoding="utf-8",
        )
        self.state = RuntimeStateStore(self.root)
        self.state.save({
            "desired_state":"on","worker_state":"starting","pid":os.getpid(),
            "started_at":"2026-09-21T00:00:00+00:00","heartbeat_at":"2026-09-21T00:00:00+00:00",
            "generation":51,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_identity(self):
        self.assertEqual(target_identity(knowledge_target()), "knowledge_revalidation:fact.stale")

    def test_verified_knowledge_precedes_proactive_opportunity_but_not_failure_memory(self):
        from sira.autonomous_targeting import select_autonomous_target
        memory = {
            "memory_id":"m_"+"1"*32,"kind":"failure","status":"observed",
            "category":"provider","capability":"retrieval","provider":"real",
            "occurrence_count":1,"priority_score":1,"last_seen_at":"2026-09-21T00:00:00+00:00",
        }
        opportunity = {
            "opportunity_id":"op_"+"2"*32,"fingerprint":"2"*64,"type":"complex_function",
            "path":"src/sira/retrieval.py","symbol":"load_evidence_run","priority_score":9999,
            "source_sha256":"3"*64,"evidence":{},"cooldown":{"eligible":True},
        }
        discovery = {"opportunities":[opportunity],"suppressed_cooldown_count":0}
        kwargs = dict(
            now_epoch=2_000_000_000.0,
            opportunity_discovery_fn=lambda *_a: discovery,
            knowledge_candidates_fn=lambda *_a: [knowledge_target()],
        )
        first = select_autonomous_target(self.root, memory_candidates_fn=lambda *_a:[memory], **kwargs)
        second = select_autonomous_target(self.root, memory_candidates_fn=lambda *_a:[], **kwargs)
        kwargs["knowledge_candidates_fn"] = lambda *_a: []  # Revalidation completed or cooled down.
        third = select_autonomous_target(self.root, memory_candidates_fn=lambda *_a:[], **kwargs)
        self.assertEqual(first["target"]["target_kind"], "memory")
        self.assertEqual(second["target"]["target_kind"], "knowledge_revalidation")
        self.assertEqual(third["target"]["target_kind"], "opportunity")

    def test_runtime_never_enters_writer_for_knowledge(self):
        from sira.runtime import run_unified_improvement_cycle
        writer_calls = []
        def selector(_root):
            return {"selection_id":"ats_"+"a"*32,"status":"selected","target":knowledge_target(),"alternatives":[]}
        def runner(_root, target):
            return {
                "status":"completed","outcome":"verification_refresh_required",
                "knowledge_key":target["knowledge_key"],"api_requests":0,"metered_model_requests":0,
                "promotion_performed":False,"main_tree_modified":False,
            }
        result = run_unified_improvement_cycle(
            self.root, 51, target_selector=selector, knowledge_revalidation_runner=runner,
            opportunity_handoff_runner=lambda *_a, **_k: writer_calls.append(True) or {}
        )
        self.assertEqual(writer_calls, [])
        self.assertEqual(result["target_kind"], "knowledge_revalidation")
        self.assertEqual(result["outcome"], "verification_refresh_required")
        self.assertFalse(result["promotion_performed"])
        self.assertFalse(result["main_tree_modified"])


if __name__ == "__main__":
    unittest.main()
