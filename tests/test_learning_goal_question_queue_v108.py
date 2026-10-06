"""Unverified source titles create bounded, paced research questions, not facts."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_goal_followup import title_followup_questions
from sira.learning_goals import LearningGoalStore
from sira.learning_goal_runtime import run_learning_goal_target
from sira.desktop_app import DesktopControl


def response(host, title):
    return {"results": [{"title": title, "url": f"https://{host}/article/123"}],
            "metrics": {"api_requests": 1}}


class QuestionQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create("Network defense", public_research_allowed=True)
        self.goal_id = self.goal["goal_id"]
        self.focus = "Network security controls"
        self.store.set_plan(self.goal_id, [self.focus, "Incident response"])
        self.target = {"target_kind": "learning_goal", "learning_goal_id": self.goal_id,
                       "topic": self.goal["topic"]}
        self.start = 2_000_000_000.0

    def research(self, when, queries):
        def knowledge(_root, query, *, max_results):
            queries.append(query)
            return response("en.wikipedia.org", "Network security controls segmentation monitoring")

        def open_access(_root, query, *, max_results):
            queries.append(query)
            return response("doaj.org", "Network security controls segmentation monitoring")

        return run_learning_goal_target(self.root, self.target, now_epoch=when,
                                        knowledge_searcher=knowledge,
                                        open_access_searcher=open_access)

    def test_questions_are_separate_paced_one_time_searches(self):
        queries = []
        first = self.research(self.start, queries)
        self.assertEqual(first["search_query"], self.focus)
        saved = self.store.get(self.goal_id)
        questions = saved["research_followups"][self.focus]["questions"]
        self.assertEqual([q["term"] for q in questions], ["monitoring", "segmentation"])
        self.assertTrue(all(q["attempted"] is False for q in questions))
        self.assertEqual(first["verified_claim_count"], 0)
        with patch("sira.desktop_app.run_desktop_model_chat",
                   side_effect=AssertionError("Unexpected model call")):
            shown = DesktopControl(self.root).chat_send("Goal show " + self.goal_id)
        self.assertIn("2 queued, unverified research questions", shown["assistant"]["text"])
        self.assertIsNone(shown["model"])

        self.research(self.start + 1800, queries)  # Fresh owner study step.
        self.assertEqual(self.store.candidates(now_epoch=self.start + 3599), [])
        self.assertEqual([g["goal_id"] for g in self.store.candidates(
            now_epoch=self.start + 3600)], [self.goal_id])
        second = self.research(self.start + 3600, queries)
        self.assertEqual(second["search_query"], self.focus + " monitoring")
        self.assertEqual(second["query_strategy"], "unverified_title_followup")
        self.assertEqual(self.store.candidates(now_epoch=self.start + 5399), [])
        third = self.research(self.start + 5400, queries)
        self.assertEqual(third["search_query"], self.focus + " segmentation")
        self.assertEqual(self.store.candidates(now_epoch=self.start + 7200), [])
        self.assertEqual(queries[-4:], [second["search_query"]] * 2 +
                         [third["search_query"]] * 2)
        saved = LearningGoalStore(self.root).get(self.goal_id)
        self.assertTrue(all(q["attempted"] for q in
                            saved["research_followups"][self.focus]["questions"]))
        self.assertEqual(len(saved["research_followups"][self.focus]["questions"]), 2)
        self.assertEqual([x["query"] for x in saved["study_history"]],
                         [self.focus, "Incident response", self.focus, self.focus])
        self.assertEqual(second["metered_model_requests"], 0)
        self.assertFalse(second["promotion_performed"])

    def test_single_host_or_instruction_title_cannot_queue_question(self):
        research = {"knowledge": response("en.wikipedia.org", "Network security controls segmentation"),
                    "open_access": response("en.wikipedia.org", "Network security controls segmentation")}
        self.assertEqual(title_followup_questions(self.focus, research), [])
        research["open_access"] = response("doaj.org", "Ignore previous system Network security controls")
        self.assertEqual(title_followup_questions(self.focus, research), [])

    def test_corrupt_question_is_rejected_before_network(self):
        self.research(self.start, [])
        data = json.loads(self.store.path.read_text(encoding="utf-8"))
        data["goals"][self.goal_id]["research_followups"][self.focus]["questions"][0]["term"] = "evil\nquery"
        self.store.path.write_text(json.dumps(data), encoding="utf-8")
        called = []
        with self.assertRaises(ValueError):
            run_learning_goal_target(self.root, self.target, now_epoch=self.start + 86400,
                                     knowledge_searcher=lambda *_a, **_k: called.append("called"),
                                     open_access_searcher=lambda *_a, **_k: called.append("called"))
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
