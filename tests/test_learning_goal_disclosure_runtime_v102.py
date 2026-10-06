"""Official reporting guidance can support one bounded white hat claim."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.desktop_app import DesktopControl
from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goal_general_learning import collect_and_verify_goal_sources
from sira.learning_goal_disclosure_claim import assess_disclosure_claim
from sira.learning_goal_runtime import run_learning_goal_target
from sira.learning_goals import LearningGoalStore


OWASP = "https://cheatsheetseries.owasp.org/cheatsheets/Vulnerability_Disclosure_Cheat_Sheet.html"
CISA = ("https://www.cisa.gov/news-events/news/"
        "cisa-issues-final-vulnerability-disclosure-policy-directive-federal-agencies")
CLAIM = ("Vulnerability reports should provide sufficient detail for verification; "
         "disclosure policies help the public find where to send them.")
TEXT = {
    OWASP: ("Researchers should: Provide sufficient details to allow the vulnerabilities "
            "to be verified and reproduced. Once a security contact has been identified, "
            "an initial report should be made of the details of the vulnerability."),
    CISA: ("Vulnerability disclosure policies make it easier for the public to know "
           "where to send a report, what types of testing are authorized for which "
           "systems, and what communication to expect."),
}


class DisclosureRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = LearningGoalStore(self.root)
        self.topic = "White hat hacker practices and responsible disclosure"
        self.focus = "Responsible vulnerability disclosure"
        self.goal = self.store.create(self.topic, public_research_allowed=True)
        self.store.set_plan(self.goal["goal_id"], [self.focus, "Secure code review"])
        self.empty_research = {"knowledge": {"results": []}, "open_access": {"results": []}}

    def collect(self, pages=TEXT):
        return collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.topic, self.empty_research,
            fetcher=lambda url: (url, "text/html", ("<main><p>" + pages[url] + "</p></main>").encode()),
            focus=self.focus,
        )

    def test_independent_official_clauses_record_one_retrievable_claim(self):
        report = self.collect()
        self.assertEqual(report["status"], "verified_knowledge_recorded")
        self.assertEqual(report["verified_claim_count"], 1)
        self.assertEqual(report["claims"][0]["claim"], CLAIM)
        self.assertEqual(set(report["claims"][0]["source_urls"]), {OWASP, CISA})
        self.assertEqual(report["api_requests"], 2)
        self.assertEqual(report["metered_model_requests"], 0)
        with patch("sira.desktop_app.run_desktop_model_chat",
                   side_effect=AssertionError("No model needed")):
            detail = DesktopControl(self.root).chat_send("Goal show " + self.goal["goal_id"])
        self.assertIn("Progress: 1 of 2 steps", detail["assistant"]["text"])
        self.assertEqual(self.store.get(self.goal["goal_id"])["study_step_stats"][0]["attempt_count"], 0)

    def test_missing_reporting_guidance_does_not_create_verified_knowledge(self):
        pages = dict(TEXT)
        pages[CISA] = "This policy describes organizational cybersecurity responsibilities. " * 3
        report = self.collect(pages)
        self.assertEqual(report["verified_claim_count"], 0)
        self.assertEqual(report["status"], "unverified_source_statements")
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_same_pages_do_not_inflate_evidence_on_repeated_cycle(self):
        self.collect()
        self.collect()
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["knowledge_evidence"], 2)

    def test_malformed_saved_host_is_rejected_before_recording(self):
        result = self.collect({**TEXT, CISA: "An unrelated policy statement. " * 4})
        path = Path(result["artifact"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["documents"][0]["host"] = []
        path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaises(ValueError):
            assess_disclosure_claim(self.root, self.goal["goal_id"], path)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_changed_saved_document_digest_is_rejected(self):
        result = self.collect({**TEXT, CISA: "An unrelated policy statement. " * 4})
        path = Path(result["artifact"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["documents"][0]["text"] += " altered"
        path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaises(ValueError):
            assess_disclosure_claim(self.root, self.goal["goal_id"], path)

    def test_empty_metadata_still_reads_official_pages_and_records_cycle(self):
        target = {"target_kind": "learning_goal", "learning_goal_id": self.goal["goal_id"],
                  "topic": self.topic}
        empty = lambda *_a, **_k: {"results": [], "metrics": {"api_requests": 0}}
        report = run_learning_goal_target(
            self.root, target, now_epoch=2_000_000_000.0,
            knowledge_searcher=empty, open_access_searcher=empty,
            document_fetcher=lambda url: (url, "text/html", ("<main><p>" + TEXT[url] + "</p></main>").encode()),
        )
        self.assertEqual(report["outcome"], "verified_knowledge_recorded")
        self.assertEqual(report["api_requests"], 2)
        self.assertFalse(report["main_tree_modified"])
        self.assertEqual(report["metered_model_requests"], 0)


if __name__ == "__main__":
    unittest.main()
