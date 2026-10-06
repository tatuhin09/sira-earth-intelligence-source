"""A narrow claim requires two independently sourced, exact components."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.knowledge_runtime import knowledge_context_for_query
from sira.learning_goal_claim_verification import assess_discovery_claim, record_discovery_claim
from sira.learning_goal_official_sources import collect_official_learning_sources
from sira.learning_goal_relevance_replay import replay_official_learning_quotes
from sira.learning_goals import LearningGoalStore


class LearningGoalClaimVerificationV077Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.goal_id = LearningGoalStore(self.root).create(
            "Python software testing", public_research_allowed=True
        )["goal_id"]
        self.pages = {
            "docs.python.org": (
                '<main><p>Unittest supports simple test discovery. In order to be compatible with test\n'
                'discovery, all of the test files must be modules importable from the top-level directory.</p>'
                '<p>A test case is the individual unit of testing. It checks for a specific response to a given input.</p></main>'
            ),
            "docs.pytest.org": (
                '<main><p>pytest discovers all tests following its Conventions for Python test discovery, '
                'so it finds both test_ prefixed functions.</p>'
                '<p>pytest also provides a number of utilities to make writing tests easier.</p></main>'
            ),
        }

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        def fetch(url):
            return url, "text/html", self.pages[urlsplit(url).hostname].encode("utf-8")

        collect_official_learning_sources(self.root, self.goal_id, fetcher=fetch)
        replay_official_learning_quotes(self.root, self.goal_id)

    def test_dry_assessment_has_two_exact_support_components_and_does_not_learn(self):
        self.prepare()
        report = assess_discovery_claim(self.root, self.goal_id)
        self.assertEqual(report["status"], "joint_source_supported")
        self.assertEqual({x["source_id"] for x in report["support"]}, {"S1", "S2"})
        self.assertEqual(len({x["host"] for x in report["support"]}), 2)
        self.assertEqual(report["claim"], "Python unittest and pytest both support test discovery.")
        self.assertEqual((report["api_requests"], report["metered_model_requests"]), (0, 0))
        self.assertFalse(report["knowledge_written"])
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())
        self.assertEqual(assess_discovery_claim(self.root, self.goal_id), report)

    def test_record_is_idempotent_and_creates_retrievable_verified_knowledge(self):
        self.prepare()
        first = record_discovery_claim(self.root, self.goal_id)
        self.assertEqual(first["status"], "consolidated")
        self.assertEqual(first["new_evidence_count"], 2)
        self.assertEqual(first["decision"]["host_count"], 2)
        self.assertFalse(first["promotion_performed"])
        self.assertTrue(first["knowledge_written"])
        second = record_discovery_claim(self.root, self.goal_id)
        self.assertEqual(second["new_evidence_count"], 0)
        store = KnowledgeConsolidationStore(self.root)
        self.assertEqual(store.stats()["knowledge_evidence"], 2)
        results = knowledge_context_for_query(self.root, "Python test discovery")
        self.assertEqual(results["result_count"], 1)
        self.assertEqual(results["results"][0]["claim"], first["claim"])

    def test_one_missing_component_cannot_be_recorded_as_verified(self):
        self.pages["docs.pytest.org"] = (
            '<main><p>pytest also provides a number of utilities to make writing tests easier, '
            'including helpers for comparing approximate floating point values.</p></main>'
        )
        self.prepare()
        report = assess_discovery_claim(self.root, self.goal_id)
        self.assertEqual(report["status"], "insufficient_joint_support")
        self.assertEqual([x["source_id"] for x in report["support"]], ["S1"])
        with self.assertRaises(ValueError):
            record_discovery_claim(self.root, self.goal_id)
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())

    def test_modified_quote_artifact_is_rejected_before_knowledge_write(self):
        self.prepare()
        path = next((self.root / "memory/learning_goal_evidence").glob("*_official_relevant_v3.json"))
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["quotes"][0]["quote"] = "Unittest supports simple test discovery."
        path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(ValueError):
            record_discovery_claim(self.root, self.goal_id)
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
