"""A historical promotion cannot suppress a target after its edit was undone."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.autonomous_targeting import OpportunityLineageStore, opportunity_lineage_key


NOW = 2_000_000_000.0
BEFORE = b"def target():\n    return 1\n"
AFTER = b"def target():\n    return 2\n"
HANDOFF = "ohf_" + "a" * 32
RESEARCH = "or_" + "b" * 32
EVIDENCE = "oe_" + "c" * 32


class RevertedEngineeringPromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "src/sira/example.py"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(BEFORE)
        base = self.root / "improvements/opportunities"
        for name in ("handoffs", "research", "evidence"):
            (base / name).mkdir(parents=True)
        self.opportunity = {
            "opportunity_id": "op_" + "1" * 32,
            "fingerprint": "2" * 64,
            "type": "complex_function",
            "path": "src/sira/example.py",
            "symbol": "target",
            "source_sha256": hashlib.sha256(BEFORE).hexdigest(),
        }
        self.cooldown = {
            "opportunity_id": self.opportunity["opportunity_id"],
            "type": self.opportunity["type"],
            "path": self.opportunity["path"],
            "symbol": self.opportunity["symbol"],
            "outcome": "promotion_committed",
            "attempted_at_epoch": NOW - 60,
        }
        self.handoff = {
            "schema_version": 1,
            "kind": "opportunity_writer_handoff",
            "runtime_route": "protected_engineering_v1",
            "handoff_id": HANDOFF,
            "research_id": RESEARCH,
            "opportunity_id": self.opportunity["opportunity_id"],
            "status": "promoted",
            "outcome": "promotion_committed",
            "promotion_performed": True,
            "promotion": {
                "schema": "sira.engineering_promotion_report.v1",
                "promotion_performed": True,
                "changed_files": [self.opportunity["path"]],
            },
            "cooldown": self.cooldown,
        }
        (base / "evidence" / f"{EVIDENCE}.json").write_text(json.dumps({
            "kind": "opportunity_evidence_brief", "evidence_id": EVIDENCE,
            "opportunity": self.opportunity,
        }), encoding="utf-8")
        (base / "research" / f"{RESEARCH}.json").write_text(json.dumps({
            "kind": "opportunity_free_research_brief", "research_id": RESEARCH,
            "evidence_id": EVIDENCE,
        }), encoding="utf-8")
        self.handoff_path = base / "handoffs" / f"{HANDOFF}.json"
        self.handoff_path.write_text(json.dumps(self.handoff), encoding="utf-8")

    def test_reverted_promotion_is_not_imported_as_current_success(self):
        store = OpportunityLineageStore(self.root)
        self.assertEqual(store.import_handoffs(), 0)
        self.assertFalse(store._load())
        self.assertTrue(store.state(self.opportunity, now_epoch=NOW)["eligible"])
        self.assertEqual(json.loads(self.handoff_path.read_text()), self.handoff)

    def test_imported_success_ceases_suppressing_if_source_reverts(self):
        self.source.write_bytes(AFTER)
        store = OpportunityLineageStore(self.root)
        self.assertEqual(store.import_handoffs(), 1)
        self.assertFalse(store.state(self.opportunity, now_epoch=NOW)["eligible"])
        self.source.write_bytes(BEFORE)
        current = store.state(self.opportunity, now_epoch=NOW)
        self.assertTrue(current["eligible"])
        self.assertEqual(current["recent_promotion_count"], 0)
        self.assertEqual(current["priority_penalty"], 0)
        self.assertEqual(len(store._load()[current["lineage_key"]]["promotions"]), 1)

    def test_mismatched_provenance_cannot_hide_historical_success(self):
        self.handoff["opportunity_id"] = "op_" + "9" * 32
        self.handoff_path.write_text(json.dumps(self.handoff), encoding="utf-8")
        store = OpportunityLineageStore(self.root)
        self.assertEqual(store.import_handoffs(), 1)
        self.assertFalse(store.state(self.opportunity, now_epoch=NOW)["eligible"])

    def test_recorded_reversion_cannot_resurrect_after_future_source_edit(self):
        store = OpportunityLineageStore(self.root)
        self.assertTrue(store.record_source_reversion(HANDOFF))
        self.assertFalse(store.record_source_reversion(HANDOFF))
        self.source.write_bytes(AFTER)
        self.assertEqual(store.import_handoffs(), 0)
        state = store.state(self.opportunity, now_epoch=NOW)
        self.assertTrue(state["eligible"])
        self.assertEqual(state["recent_promotion_count"], 0)
        self.assertEqual(json.loads(self.handoff_path.read_text()), self.handoff)

    def test_recorded_reversion_masks_already_imported_promotion(self):
        store = OpportunityLineageStore(self.root)
        self.source.write_bytes(AFTER)
        self.assertEqual(store.import_handoffs(), 1)
        self.source.write_bytes(BEFORE)
        self.assertTrue(store.record_source_reversion(HANDOFF))
        self.source.write_bytes(AFTER)
        self.assertEqual(store.import_handoffs(), 0)
        self.assertTrue(store.state(self.opportunity, now_epoch=NOW)["eligible"])
        events = store._load()[opportunity_lineage_key(self.opportunity)]["promotions"]
        self.assertEqual({e["outcome"] for e in events}, {"promotion_committed", "promotion_reverted"})

    def test_reversion_requires_current_source_proof(self):
        self.source.write_bytes(AFTER)
        store = OpportunityLineageStore(self.root)
        with self.assertRaises(ValueError):
            store.record_source_reversion(HANDOFF)
        self.assertFalse(store._load())


if __name__ == "__main__":
    unittest.main()
