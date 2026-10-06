"""Desktop chat teaches fresh verified local claims without a model request."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.desktop_app import DesktopControl
from sira.knowledge_consolidation import KnowledgeConsolidationStore


class DesktopVerifiedKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def add_discovery_claim(self, *, both: bool = True):
        store = KnowledgeConsolidationStore(self.root)
        sources = [
            ("S1", "https://docs.python.org/3/library/unittest.html"),
            ("S2", "https://docs.pytest.org/en/stable/getting-started.html"),
        ]
        for source_id, url in sources if both else sources[:1]:
            store.record_evidence(
                "claim.python.discovery", "Python unittest and pytest both support test discovery.",
                evidence_id="evidence_" + source_id, source_id=source_id, source_url=url,
                confidence=.85, verifier_kind="verified_quote", evidence_sha256="a" * 64,
                retrieved_at="2026-09-25T00:00:00+00:00", verified=True,
            )
        store.consolidate("claim.python.discovery")

    def test_specific_question_returns_claim_and_both_sources_without_model(self):
        self.add_discovery_claim()
        control = DesktopControl(self.root)
        with patch("sira.desktop_app.run_desktop_model_chat") as model:
            reply = control.chat_send("Teach me about Python test discovery")
        self.assertEqual(reply["mode"], "local_verified_knowledge")
        text = reply["assistant"]["text"]
        self.assertIn("Python unittest and pytest both support test discovery.", text)
        self.assertIn("https://docs.python.org/3/library/unittest.html", text)
        self.assertIn("https://docs.pytest.org/en/stable/getting-started.html", text)
        self.assertIn("one verified claim", text.casefold())
        model.assert_not_called()

    def test_broad_topic_reply_is_explicitly_partial(self):
        self.add_discovery_claim()
        reply = DesktopControl(self.root).chat_send("Teach me Python software testing")
        self.assertEqual(reply["mode"], "local_verified_knowledge")
        self.assertIn("not cover the whole topic", reply["assistant"]["text"].casefold())

    def test_unknown_memory_question_does_not_ask_model_to_invent_learning(self):
        self.add_discovery_claim()
        with patch("sira.desktop_app.run_desktop_model_chat") as model:
            reply = DesktopControl(self.root).chat_send("What did you learn about quantum chemistry?")
        self.assertEqual(reply["mode"], "local_knowledge_unavailable")
        self.assertIn("no matching verified local claim", reply["assistant"]["text"].casefold())
        model.assert_not_called()

    def test_one_source_pending_claim_cannot_be_taught_as_verified(self):
        self.add_discovery_claim(both=False)
        fake = {"status": "completed", "reply": "Normal guarded reply.",
                "intent": "conversation", "needs_research": False,
                "access_request_id": None, "metrics": {"api_requests": 1}, "artifact": None}
        with patch("sira.desktop_app.run_desktop_model_chat", return_value=fake):
            reply = DesktopControl(self.root).chat_send("Teach me about Python test discovery")
        self.assertEqual(reply["mode"], "model")


if __name__ == "__main__":
    unittest.main()
