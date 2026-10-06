from pathlib import Path
import sys, tempfile, unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.knowledge_runtime import (
    KnowledgeRevalidationLedger, knowledge_revalidation_targets,
    revalidate_knowledge_target, resolve_knowledge_query,
)


class KnowledgeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = KnowledgeConsolidationStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, key, claim, when):
        for eid, url, sha in (
            ("x:1", "https://one.example/a", "a"*64),
            ("x:2", "https://two.example/a", "b"*64),
        ):
            self.store.record_evidence(
                key, claim, evidence_id=eid, source_id=eid, source_url=url,
                confidence=.9, verifier_kind="fixture", evidence_sha256=sha,
                retrieved_at=when, verified=True,
            )
        self.store.consolidate(key, stale_after_days=30)

    def test_fresh_reuse_skips_network(self):
        self.seed("fact.venv", "Virtual environments isolate project package installations.", "2026-09-20T00:00:00+00:00")
        calls = []
        result = resolve_knowledge_query(
            self.root, "virtual environments package installations",
            allow_research_fallback=True,
            knowledge_searcher=lambda *_a, **_k: calls.append("k"),
            open_access_searcher=lambda *_a, **_k: calls.append("o"),
            now="2026-09-21T00:00:00+00:00",
        )
        self.assertEqual(result["mode"], "local_reuse")
        self.assertEqual(calls, [])
        self.assertFalse(result["promotion_authorized"])

    def test_stale_revalidation_is_discovery_only_and_cooled_down(self):
        self.seed("fact.stale", "A stale verified claim needs revalidation.", "2025-01-01T00:00:00+00:00")
        target = next(row for row in knowledge_revalidation_targets(self.root, now_epoch=1_790_000_000.0) if row["knowledge_key"] == "fact.stale")
        self.assertEqual(target["priority_class"], 2)
        fake = lambda *_a, **_k: {"results": [{"title": "source"}], "metrics": {"api_requests": 0}}
        result = revalidate_knowledge_target(
            self.root, target, knowledge_searcher=fake, open_access_searcher=fake,
            now_epoch=1_790_000_000.0
        )
        self.assertEqual(result["outcome"], "verification_refresh_required")
        self.assertFalse(result["refresh_performed"])
        self.assertTrue(result["verified_synthesis_required"])
        self.assertFalse(result["promotion_performed"])
        state = KnowledgeRevalidationLedger(self.root).state("fact.stale", now_epoch=1_790_000_001.0)
        self.assertFalse(state["eligible"])
        self.assertNotIn("fact.stale", {row["knowledge_key"] for row in knowledge_revalidation_targets(self.root, now_epoch=1_790_000_001.0)})


if __name__ == "__main__":
    unittest.main()
