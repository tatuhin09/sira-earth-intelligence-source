"""A84.1: current dashboard state and historical chat stay distinguishable."""
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.desktop_app import DesktopControl
from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.runtime import RuntimeStateStore
from sira.self_model import collect_self_model


CLAIM = "Urban tree canopy can reduce daytime heat in neighborhoods by providing shade."


class DemoConsistencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="sira-a841-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def add_evidence(self, kind):
        store = KnowledgeConsolidationStore(self.root)
        now = datetime.now(timezone.utc).isoformat()
        for index, host in enumerate(("www.nasa.gov", "www.noaa.gov")):
            store.record_evidence("claim.tree", CLAIM, evidence_id=f"e{index}",
                                  source_id=f"s{index}", source_url=f"https://{host}/trees",
                                  confidence=.91, verifier_kind=kind,
                                  evidence_sha256=chr(97 + index) * 64,
                                  retrieved_at=now, verified=True)
        self.assertEqual(store.consolidate("claim.tree").status, "consolidated")

    def test_current_two_host_research_proof_is_only_narrow_partial_capability(self):
        self.add_evidence("general_exact_sentence_two_hosts_v1")
        model = collect_self_model(self.root)
        row = model["capabilities"]["research"]
        self.assertEqual(row["state"], "partially_demonstrated")
        self.assertEqual(row["evidence"][0]["ref"], "claim.tree")
        self.assertEqual(len(row["evidence"][0]["source_ids"]), 2)
        self.assertFalse(model["authority_granted"])

    def test_unrelated_or_unverified_source_does_not_claim_research(self):
        self.add_evidence("bootstrap_exact_excerpt_two_host_v1")
        self.assertEqual(collect_self_model(self.root)["capabilities"]["research"]["state"], "unverified")

    def test_overview_uses_one_core_runtime_and_head_bound_release(self):
        RuntimeStateStore(self.root).save({"desired_state": "off", "worker_state": "stopped", "generation": 68})
        control = DesktopControl(self.root)
        fresh = RuntimeStateStore(self.root).status()
        stale = {**fresh, "generation": 8, "effective_state": "running"}
        with patch.object(RuntimeStateStore, "status", side_effect=[fresh, stale]), \
             patch("sira.desktop_app._latest_json", return_value={
                 "release_ready": True, "git": {"revision": "oldhead"},
                 "required_check_count": 19, "required_checks_passed": 19}):
            view = control.overview()
        self.assertEqual(view["runtime"]["generation"], 68)
        self.assertEqual(view["runtime"], view["core"]["runtime"])
        self.assertEqual(view["release"]["release_ready"], view["core"]["release"]["release_ready"])
        self.assertFalse(view["release"]["release_ready"])

    def test_saved_status_chat_is_labelled_history_in_both_views(self):
        static = Path(__file__).resolve().parents[1] / "desktop/static"
        html = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="chat-current-status"', html)
        self.assertIn("Saved reply •", script)
        self.assertIn("Historical snapshot", script)
        self.assertIn("Current SIRA status", script)

    def test_worker_alive_without_cycle_is_not_described_as_stopped(self):
        script = (Path(__file__).resolve().parents[1] / "desktop/static/app.js").read_text()
        self.assertIn("Runtime worker alive / no active cycle", script)


if __name__ == "__main__":
    unittest.main()
