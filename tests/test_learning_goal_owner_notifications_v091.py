"""Learning notices report verified claims and repeated evidence gaps locally."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_goals import LearningGoalStore
from sira.learning_goal_runtime import run_learning_goal_target
from sira.owner_notifications import OwnerNotificationStore, pump_owner_notifications


class LearningGoalOwnerNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.goals = LearningGoalStore(self.root)
        self.goal = self.goals.create("Software testing", public_research_allowed=True)

    def test_two_source_verified_claim_is_delivered_once_without_model(self):
        claim = "Software testing checks whether an application behaves as expected."
        urls = ("https://en.wikipedia.org/wiki/Software_testing",
                "https://doaj.org/article/testing")

        # The runtime itself records the knowledge and the notice. The fake
        # only replaces public network responses with exact document bytes.
        target = {"target_kind": "learning_goal", "learning_goal_id": self.goal["goal_id"],
                  "topic": self.goal["topic"]}
        report = run_learning_goal_target(
            self.root, target, now_epoch=2_000_000_000,
            knowledge_searcher=lambda *_a, **_k: {"results": [{"title": "Software testing", "url": urls[0]}],
                                                   "metrics": {"api_requests": 1}},
            open_access_searcher=lambda *_a, **_k: {"results": [{"title": "Software testing", "url": urls[1]}],
                                                     "metrics": {"api_requests": 1}},
            document_fetcher=lambda url: (url, "text/html", ("<html><main><p>Software testing helps teams find defects.</p><p>" + claim + "</p></main></html>").encode()),
        )
        self.assertEqual(report["outcome"], "verified_knowledge_recorded")
        self.assertEqual(report["metered_model_requests"], 0)
        store = OwnerNotificationStore(self.root)
        events = store.iter_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["category"], "learning_verified_claim")
        self.assertIn(claim, events[0]["body"])
        delivered = []
        first = pump_owner_notifications(self.root, sender=lambda event: delivered.append(event) or True,
                                         now_epoch=2_000_000_000)
        second = pump_owner_notifications(self.root, sender=lambda event: delivered.append(event) or True,
                                          now_epoch=2_000_000_001)
        self.assertEqual((first["delivered"], second["attempted"], len(delivered)), (1, 0, 1))
        self.assertEqual(store.enqueue_learning_result(self.goals.get(self.goal["goal_id"]), report), [])
        self.assertEqual(len(OwnerNotificationStore(self.root).iter_events()), 1)

    def test_single_source_never_notifies_as_verified(self):
        target = {"target_kind": "learning_goal", "learning_goal_id": self.goal["goal_id"],
                  "topic": self.goal["topic"]}
        report = run_learning_goal_target(
            self.root, target, now_epoch=2_000_000_000,
            knowledge_searcher=lambda *_a, **_k: {"results": [{"title": "Software testing", "url": "https://en.wikipedia.org/wiki/Software_testing"}],
                                                   "metrics": {"api_requests": 1}},
            open_access_searcher=lambda *_a, **_k: {"results": [], "metrics": {"api_requests": 1}},
            document_fetcher=lambda url: (url, "text/html", b"<p>Software testing checks whether software behaves as expected.</p>"),
        )
        self.assertEqual(report["verified_claim_count"], 0)
        self.assertEqual(OwnerNotificationStore(self.root).iter_events(), ())

    def test_unconsolidated_claim_cannot_be_announced_as_verified(self):
        rows = OwnerNotificationStore(self.root).enqueue_learning_result(
            self.goals.get(self.goal["goal_id"]), {
                "learning_goal_id": self.goal["goal_id"], "topic": self.goal["topic"],
                "outcome": "verified_knowledge_recorded", "verified_claim_count": 1,
                "study_step_index": None,
                "document_review": {"claims": [{
                    "knowledge_key": "claim." + "a" * 32,
                    "claim": "Software testing proves every program is secure.",
                }]},
            })
        self.assertEqual(rows, [])
        self.assertEqual(OwnerNotificationStore(self.root).iter_events(), ())

    def test_repeated_step_gap_notifies_once_without_claiming_mastery(self):
        goal_id = self.goal["goal_id"]
        self.goals.set_plan(goal_id, ["Test discovery", "Fixtures"])
        for count in range(2):
            if count:
                self.goals.record_attempt(goal_id, "research_sources_insufficient", at_epoch=101,
                                          study_step_index=1, verified_claim_count=0)
            self.goals.record_attempt(goal_id, "research_sources_insufficient", at_epoch=100 + count,
                                      study_step_index=0, verified_claim_count=0)
            result = OwnerNotificationStore(self.root).enqueue_learning_result(
                self.goals.get(goal_id), {"learning_goal_id": goal_id,
                                          "topic": self.goal["topic"],
                                          "outcome": "research_sources_insufficient",
                                          "study_step_index": 0, "verified_claim_count": 0})
            self.assertEqual(len(result), count)
        events = OwnerNotificationStore(self.root).iter_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["category"], "learning_evidence_gap")
        self.assertIn("Test discovery", events[0]["body"])
        self.assertNotIn("mastered", events[0]["body"].lower())
        self.assertEqual(OwnerNotificationStore(self.root).enqueue_learning_result(
            self.goals.get(goal_id), {"learning_goal_id": goal_id, "topic": self.goal["topic"],
                                      "outcome": "research_sources_insufficient",
                                      "study_step_index": 0, "verified_claim_count": 0}), [])

    def test_notification_storage_failure_does_not_erase_study_result(self):
        target = {"target_kind": "learning_goal", "learning_goal_id": self.goal["goal_id"],
                  "topic": self.goal["topic"]}
        empty = lambda *_a, **_k: {"results": [], "metrics": {"api_requests": 0}}
        with patch.object(OwnerNotificationStore, "enqueue_learning_result",
                          side_effect=RuntimeError("broken outbox")):
            report = run_learning_goal_target(
                self.root, target, now_epoch=2_000_000_000,
                knowledge_searcher=empty, open_access_searcher=empty,
            )
        self.assertEqual(report["outcome"], "research_sources_insufficient")
        self.assertEqual(self.goals.get(self.goal["goal_id"])["last_outcome"], report["outcome"])
        self.assertTrue(Path(report["artifact"]).is_file())


if __name__ == "__main__":
    unittest.main()
