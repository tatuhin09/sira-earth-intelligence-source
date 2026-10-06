"""A84: desktop Chat and dashboard use the same bounded A83 Core."""
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.desktop_app import DesktopControl, DesktopRequestHandler, _Server
from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goals import LearningGoalStore
from sira.runtime import RuntimeStateStore
from sira.sira_core import SiraCore


CLAIM = "Urban tree canopy can reduce daytime heat in neighborhoods by providing shade."


class UnifiedDesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sira-a84-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        RuntimeStateStore(self.root).save({
            "desired_state": "off", "worker_state": "stopped", "generation": 67,
        })
        self.control = DesktopControl(self.root)

    def add_claim(self):
        store = KnowledgeConsolidationStore(self.root)
        now = datetime.now(timezone.utc).isoformat()
        for n, host in enumerate(("www.nasa.gov", "www.noaa.gov")):
            store.record_evidence(
                "claim.tree", CLAIM, evidence_id=f"e{n}", source_id=f"s{n}",
                source_url=f"https://{host}/trees", confidence=.91,
                verifier_kind="independent_read", evidence_sha256=chr(97+n)*64,
                retrieved_at=now, verified=True,
            )
        self.assertEqual(store.consolidate("claim.tree").status, "consolidated")

    def test_status_and_dashboard_share_identity_and_real_generation(self):
        with patch("sira.desktop_app.run_desktop_model_chat", side_effect=AssertionError("model")):
            reply = self.control.chat_send("What are you doing and what is your current state?")
        dashboard = self.control.overview()["core"]
        self.assertEqual(reply["core_task"]["route"], "self_status")
        self.assertEqual(reply["identity"]["name"], dashboard["identity"]["name"])
        self.assertEqual(dashboard["runtime"]["generation"], 67)
        self.assertEqual(dashboard["runtime"]["effective_state"], "stopped")
        self.assertEqual(RuntimeStateStore(self.root).status()["desired_state"], "off")
        self.assertEqual(dashboard["last_task"]["task_id"], reply["core_task"]["task_id"])

    def test_verified_memory_hit_has_provenance_and_zero_external_calls(self):
        self.add_claim()
        with patch("sira.desktop_app.run_desktop_model_chat", side_effect=AssertionError("model")), \
                patch("sira.sira_core.create_research_job", side_effect=AssertionError("research")):
            reply = self.control.chat_send(CLAIM)
        self.assertEqual(reply["mode"], "local_verified_knowledge")
        self.assertEqual(reply["core_task"]["route"], "verified_memory")
        self.assertEqual(reply["provenance"][0]["knowledge_key"], "claim.tree")
        self.assertEqual(len(reply["provenance"][0]["sources"]), 2)
        self.assertIn(CLAIM, reply["assistant"]["text"])
        self.assertIsNone(reply["model"])

    def test_unknown_learning_question_is_research_needed_without_fabrication(self):
        with patch("sira.desktop_app.run_desktop_model_chat", side_effect=AssertionError("model")):
            reply = self.control.chat_send("What did you learn about quantum chemistry?")
        self.assertEqual(reply["core_task"]["route"], "research_needed")
        self.assertEqual(reply["core_task"]["status"], "blocked")
        self.assertEqual(reply["mode"], "local_knowledge_unavailable")
        self.assertIn("research", reply["assistant"]["text"].casefold())
        self.assertEqual(self.control.overview()["core"]["knowledge"]["verified_count"], 0)

    def test_raw_bootstrap_or_one_weak_evidence_cannot_answer(self):
        raw = self.root / "memory/bootstrap_corpus/raw"
        raw.mkdir(parents=True)
        (raw / "sample.txt").write_text("Ocean coral fluorescence causes rain.")
        with patch("sira.desktop_app.run_desktop_model_chat", side_effect=AssertionError("model")):
            reply = self.control.chat_send("What did you learn about ocean coral fluorescence?")
        self.assertEqual(reply["core_task"]["route"], "research_needed")
        self.assertEqual(reply["provenance"], [])

    def test_explicit_research_queues_one_existing_guarded_job(self):
        with patch("sira.desktop_app.threading.Thread.start"):
            reply = self.control.chat_send("Research urban tree canopy", research=True)
        self.assertEqual(reply["status"], "started")
        self.assertEqual(reply["core_task"]["route"], "research_job")
        self.assertEqual(reply["core_task"]["artifact_refs"], [reply["job"]["job_id"]])
        self.assertEqual(len(self.control.research_jobs.list()), 1)
        self.assertEqual(reply["job"]["verification"]["status"], "pending")

    def test_completed_research_job_updates_core_task_without_claiming_verification(self):
        with patch("sira.desktop_app.threading.Thread.start"):
            started = self.control.chat_send("Research urban tree canopy", research=True)
        job = started["job"]
        job["status"] = "completed"
        job["verification"] = {"status": "insufficient_independent_sources"}
        self.control.research_jobs.save(job)
        again = DesktopControl(self.root).overview()["core"]
        self.assertEqual(again["last_task"]["status"], "completed")
        self.assertEqual(again["last_task"]["route"], "research_job")
        self.assertEqual(again["last_task"]["verification_status"], "insufficient_independent_sources")
        self.assertEqual(again["knowledge"]["verified_count"], 0)

    def test_blocked_research_job_is_not_left_pending_after_reload(self):
        with patch("sira.desktop_app.threading.Thread.start"):
            started = self.control.chat_send("Research urban tree canopy", research=True)
        job = started["job"]
        job["status"] = "blocked"
        job["verification"] = {"status": "provider_unavailable"}
        self.control.research_jobs.save(job)
        again = DesktopControl(self.root).overview()["core"]
        self.assertEqual(again["last_task"]["status"], "blocked")
        self.assertEqual(again["last_task"]["verification_status"], "provider_unavailable")

    def test_priority_and_engineering_remain_guarded(self):
        goal = LearningGoalStore(self.root).create("Python software testing")
        background = SiraCore(self.root).dispatch("learn", goal_id=goal["goal_id"], origin="background")
        with patch("sira.engineering_runtime.run_engineering_runtime_handoff", side_effect=AssertionError("unsafe")):
            reply = self.control.chat_send("Improve SIRA code safely")
        self.assertEqual(reply["core_task"]["route"], "protected_engineering_pipeline")
        self.assertEqual(reply["core_task"]["status"], "pending")
        dashboard = self.control.overview()["core"]
        self.assertEqual(dashboard["pending_work"][0]["task_id"], reply["core_task"]["task_id"])
        self.assertEqual(dashboard["pending_work"][1]["task_id"], background["task"]["task_id"])
        self.assertFalse(dashboard["authority_granted"])
        self.assertFalse(dashboard["promotion_authorized"])
        self.assertFalse(dashboard["paid_spending_authorized"])
        self.assertFalse(dashboard["skill_activated"])

    def test_goal_provider_dashboard_is_bounded_and_secret_free(self):
        goal = LearningGoalStore(self.root).create("Python software testing")
        (self.root / ".env").write_text("GEMINI_API_KEY=super-secret-test-value\n")
        snapshot = self.control.overview()["core"]
        self.assertEqual(snapshot["learning"]["active_goal_count"], 1)
        self.assertEqual(snapshot["learning"]["goals"][0]["goal_id"], goal["goal_id"])
        self.assertNotIn("super-secret-test-value", json.dumps(snapshot))
        self.assertLessEqual(len(snapshot["providers"]["entries"]), 16)

    def test_task_history_survives_desktop_reload(self):
        reply = self.control.chat_send("What is your status?")
        again = DesktopControl(self.root).overview()["core"]
        self.assertEqual(again["last_task"]["task_id"], reply["core_task"]["task_id"])

    def test_provider_outage_keeps_local_status_available(self):
        with patch("sira.self_model.build_provider_health_snapshot", side_effect=OSError("offline")):
            reply = self.control.chat_send("What is your status?")
            dashboard = self.control.overview()["core"]
        self.assertEqual(reply["core_task"]["route"], "self_status")
        self.assertEqual(dashboard["providers"]["known_count"], 0)
        self.assertFalse(dashboard["paid_spending_authorized"])

    def test_actual_desktop_http_bridge_uses_shared_core_and_session_guard(self):
        static = Path(__file__).resolve().parents[1] / "desktop/static"
        with _Server(("127.0.0.1", 0), DesktopRequestHandler, root=self.root,
                     static_dir=static, session_token="local-test-token") as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.shutdown)
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/api/overview", timeout=5) as response:
                dashboard = json.load(response)
            req = Request(base + "/api/chat", method="POST",
                          data=json.dumps({"message": "What is your status?"}).encode(),
                          headers={"Content-Type": "application/json", "X-SIRA-Session": "local-test-token"})
            with urlopen(req, timeout=5) as response:
                reply = json.load(response)
            self.assertEqual(dashboard["core"]["identity"]["name"], reply["identity"]["name"])
            self.assertEqual(reply["core_task"]["route"], "self_status")
            self.assertEqual(dashboard["runtime"]["generation"], 67)

    def test_ui_has_live_core_cards_and_no_fake_test_baseline(self):
        static = Path(__file__).resolve().parents[1] / "desktop/static"
        html = (static / "index.html").read_text()
        script = (static / "app.js").read_text()
        self.assertIn('id="core-current-task"', html)
        self.assertIn('id="core-capabilities"', html)
        self.assertIn('id="core-goals"', html)
        self.assertIn('id="core-providers"', html)
        self.assertIn('data.core', script)
        self.assertNotIn("663 / 663", html + script)


if __name__ == "__main__":
    unittest.main()
