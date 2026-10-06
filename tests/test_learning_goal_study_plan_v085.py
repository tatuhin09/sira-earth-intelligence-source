"""Owner learning goals retain a bounded, rotating study program."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.cli import main
from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goals import GOAL_RETRY_SECONDS, LearningGoalStore
from sira.learning_goal_general_learning import collect_and_verify_goal_sources
from sira.learning_goal_runtime import run_learning_goal_target


WIKI = "https://en.wikipedia.org/wiki/Network_security"
DOAJ = "https://doaj.org/article/network"
CLAIM = "Network security controls protect data from unauthorized access."


class LearningGoalStudyPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create(
            "Cybersecurity foundations and defense", priority="high",
            public_research_allowed=True,
        )
        self.target = {"target_kind": "learning_goal", "learning_goal_id": self.goal["goal_id"],
                       "topic": self.goal["topic"]}

    def set_plan(self, steps):
        method = getattr(self.store, "set_plan", None)
        self.assertTrue(callable(method), "Existing goals must accept a study plan")
        return method(self.goal["goal_id"], steps)

    def test_plan_is_persistent_idempotent_and_cannot_silently_change(self):
        steps = ["Network security controls", "Identity and access management"]
        capture = io.StringIO()
        with redirect_stdout(capture):
            try:
                code = main(["--root", str(self.root), "goals", "set-plan",
                             self.goal["goal_id"], *steps])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 0)
        saved = json.loads(capture.getvalue())
        self.assertEqual(saved["study_plan"], steps)
        self.assertEqual(saved["study_next_index"], 0)
        self.assertEqual(self.set_plan(steps), saved)
        self.assertEqual(LearningGoalStore(self.root).get(self.goal["goal_id"])["study_plan"], steps)
        with self.assertRaises(ValueError):
            self.set_plan(["Different research question"])
        self.assertEqual(self.store.get(self.goal["goal_id"])["study_plan"], steps)

    def test_failed_steps_are_recorded_and_next_day_uses_next_query(self):
        self.set_plan(["Network security controls", "Identity and access management"])
        searches = []

        def search(_root, query, *, max_results):
            searches.append(query)
            return {"status": "failed", "results": [], "provider_failures": [],
                    "metrics": {"api_requests": 0}}

        first = run_learning_goal_target(
            self.root, self.target, now_epoch=2_000_000_000.0,
            knowledge_searcher=search, open_access_searcher=search,
        )
        self.assertEqual(first["research_query"], "Network security controls")
        self.assertEqual(first["outcome"], "research_sources_insufficient")
        self.assertEqual(self.store.get(self.goal["goal_id"])["study_next_index"], 1)
        self.assertNotIn(self.goal["goal_id"],
                         {row["goal_id"] for row in self.store.candidates(now_epoch=2_000_000_001)})

        second = run_learning_goal_target(
            self.root, self.target, now_epoch=2_000_000_000.0 + GOAL_RETRY_SECONDS,
            knowledge_searcher=search, open_access_searcher=search,
        )
        self.assertEqual(second["research_query"], "Identity and access management")
        self.assertEqual(searches, ["Network security controls"] * 2 +
                         ["Identity and access management"] * 2)
        saved = self.store.get(self.goal["goal_id"])
        self.assertEqual(saved["study_next_index"], 0)
        self.assertEqual([x["query"] for x in saved["study_history"]],
                         ["Network security controls", "Identity and access management"])
        self.assertEqual([x["outcome"] for x in saved["study_history"]],
                         ["research_sources_insufficient"] * 2)
        self.assertEqual(saved["last_report"], second["artifact"])
        self.assertEqual(first["verified_claim_count"], 0)

    def test_planned_query_drives_source_ranking_and_exact_claim_verification(self):
        self.set_plan(["Network security controls", "Identity and access management"])
        searches = []

        def knowledge(_root, query, *, max_results):
            searches.append(query)
            return {"status": "completed", "results": [
                {"title": "Network security controls", "url": WIKI}],
                "metrics": {"api_requests": 1}}

        def open_access(_root, query, *, max_results):
            searches.append(query)
            return {"status": "completed", "results": [
                {"title": "Network security controls", "url": DOAJ}],
                "metrics": {"api_requests": 1}}

        result = run_learning_goal_target(
            self.root, self.target, now_epoch=2_000_000_000.0,
            knowledge_searcher=knowledge, open_access_searcher=open_access,
            document_fetcher=lambda url: (url, "text/html",
                ("<html><main><p>" + CLAIM + "</p></main></html>").encode()),
        )
        self.assertEqual(searches, ["Network security controls"] * 2)
        self.assertEqual(result["research_query"], "Network security controls")
        self.assertEqual(result["verified_claim_count"], 1)
        self.assertEqual(result["outcome"], "verified_knowledge_recorded")
        rows = KnowledgeConsolidationStore(self.root).search("Network security", limit=5)
        self.assertEqual(rows[0]["claim_text"], CLAIM)
        self.assertEqual(rows[0]["host_count"], 2)
        self.assertEqual(self.store.get(self.goal["goal_id"])["study_history"][0]["verified_claim_count"], 1)

    def test_corrupt_plan_cursor_fails_closed(self):
        self.set_plan(["Network security controls", "Identity and access management"])
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        payload["goals"][self.goal["goal_id"]]["study_next_index"] = 99
        self.store.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.get(self.goal["goal_id"])

    def test_adding_plan_does_not_bypass_existing_daily_retry(self):
        self.store.record_attempt(self.goal["goal_id"], "research_sources_insufficient",
                                  at_epoch=2_000_000_000.0, research_policy_version=2)
        self.set_plan(["Network security controls", "Identity and access management"])
        self.assertEqual(self.store.candidates(now_epoch=2_000_000_001), [])
        self.assertEqual(run_learning_goal_target(
            self.root, self.target, now_epoch=2_000_000_001,
        )["outcome"], "goal_cooldown")
        self.assertEqual(self.store.get(self.goal["goal_id"])["study_next_index"], 0)

    def test_unplanned_query_cannot_be_read_as_owner_authorized(self):
        self.set_plan(["Network security controls", "Identity and access management"])
        with self.assertRaises(ValueError):
            collect_and_verify_goal_sources(
                self.root, self.goal["goal_id"], self.goal["topic"], {},
                focus="Unrequested public research topic",
                fetcher=lambda _: self.fail("No network request authorized"),
            )


if __name__ == "__main__":
    unittest.main()
