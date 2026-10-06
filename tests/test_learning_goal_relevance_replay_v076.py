"""Offline evidence replay selects explanatory text, never navigation or facts."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_goal_official_sources import collect_official_learning_sources
from sira.learning_goal_relevance_replay import replay_official_learning_quotes
from sira.learning_goals import LearningGoalStore


class LearningGoalRelevanceReplayV076Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.goal_id = LearningGoalStore(self.root).create(
            "Python software testing", public_research_allowed=True
        )["goal_id"]
        self.pages = {
            "docs.python.org": (
                '<main><h2>The Python Testing Tools Taxonomy</h2>'
                '<p>An extensive list of Python testing tools including functional testing frameworks.</p>'
                '<p>The unittest unit testing framework supports test automation, sharing of setup and shutdown code, and aggregation of tests into collections.</p>'
                '<p>A test case is the individual unit of testing. It checks for a specific response to a particular set of inputs.</p>'
                '</main>'
            ),
            "docs.pytest.org": (
                '<main><pre>platform linux -- Python 3.x.y, pytest-9.x.y, pluggy-1.x.y</pre>'
                '<p>pytest discovers all tests following its conventions for Python test discovery, so it finds test_ prefixed functions automatically.</p>'
                '<p>The pytest framework allows you to use the assert statement to verify expectations in your tests.</p>'
                '</main>'
            ),
        }

        def fetch(url):
            host = urlsplit(url).hostname
            return url, "text/html", self.pages[host].encode("utf-8")

        self.original = collect_official_learning_sources(self.root, self.goal_id, fetcher=fetch)

    def tearDown(self):
        self.temp.cleanup()

    def test_replay_replaces_boilerplate_with_exact_explanatory_quotes_without_requests(self):
        report = replay_official_learning_quotes(self.root, self.goal_id)
        self.assertEqual(report["status"], "relevant_quotes_need_claim_verification")
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(report["metered_model_requests"], 0)
        self.assertEqual(report["distinct_quote_host_count"], 2)
        self.assertFalse(report["verified_claims_recorded"])
        text = " ".join(q["quote"] for q in report["quotes"])
        self.assertIn("unittest unit testing framework supports test automation", text)
        self.assertIn("pytest discovers all tests", text)
        self.assertNotIn("Testing Tools Taxonomy", text)
        self.assertNotIn("platform linux", text)
        for quote in report["quotes"]:
            source = self.original["sources"][int(quote["source_id"][1:]) - 1]
            self.assertEqual(source["text"][quote["start"]:quote["end"]], quote["quote"])
            self.assertEqual(quote["content_sha256"], source["content_sha256"])
            self.assertFalse(quote["verified"])
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())
        self.assertEqual(replay_official_learning_quotes(self.root, self.goal_id), report)

    def test_mutated_source_text_is_rejected_before_replay(self):
        path = Path(self.original["artifact"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["sources"][0]["text"] = "A fabricated explanation"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            replay_official_learning_quotes(self.root, self.goal_id)

    def test_changed_quote_artifact_is_not_silently_replaced(self):
        report = replay_official_learning_quotes(self.root, self.goal_id)
        path = Path(report["artifact"])
        altered = json.loads(path.read_text(encoding="utf-8"))
        altered["quotes"][0]["quote"] = "fabricated"
        path.write_text(json.dumps(altered), encoding="utf-8")
        with self.assertRaises(ValueError):
            replay_official_learning_quotes(self.root, self.goal_id)

    def test_no_explanatory_text_stays_unverified(self):
        self.pages["docs.python.org"] = '<main><p>' + ('Python testing tools reference. ' * 4) + '</p></main>'
        self.pages["docs.pytest.org"] = '<main><p>' + ('Python testing tools reference. ' * 4) + '</p></main>'
        # A fresh root prevents replacing the evidence already saved in setUp.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            goal_id = LearningGoalStore(root).create("Python software testing", public_research_allowed=True)["goal_id"]

            def fetch(url):
                host = urlsplit(url).hostname
                return url, "text/html", self.pages[host].encode("utf-8")

            collect_official_learning_sources(root, goal_id, fetcher=fetch)
            report = replay_official_learning_quotes(root, goal_id)
            self.assertEqual(report["status"], "insufficient_relevant_documentation")
            self.assertEqual(report["quotes"], [])
            self.assertFalse(report["verified_claims_recorded"])

    def test_source_formatting_newline_does_not_cut_off_a_sentence(self):
        self.pages["docs.python.org"] = (
            '<main><p>Unittest supports simple test discovery. In order to be compatible with test\n'
            'discovery, test files must be importable from the top-level directory.</p>'
            '<p>A test case is the individual unit of testing. It checks for a specific\n'
            'response to a particular set of inputs.</p></main>'
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            goal_id = LearningGoalStore(root).create("Python software testing", public_research_allowed=True)["goal_id"]

            def fetch(url):
                return url, "text/html", self.pages[urlsplit(url).hostname].encode("utf-8")

            original = collect_official_learning_sources(root, goal_id, fetcher=fetch)
            report = replay_official_learning_quotes(root, goal_id)
            python_quotes = [q for q in report["quotes"] if q["source_id"] == "S1"]
            self.assertEqual(len(python_quotes), 2)
            self.assertTrue(all(q["quote"].endswith(".") for q in python_quotes))
            self.assertTrue(any("test\ndiscovery, test files" in q["quote"] for q in python_quotes))
            self.assertTrue(any("specific\nresponse to a particular set of inputs." in q["quote"]
                                for q in python_quotes))
            for quote in python_quotes:
                text = original["sources"][0]["text"]
                self.assertEqual(text[quote["start"]:quote["end"]], quote["quote"])

    def test_new_selection_keeps_previous_artifact_untouched(self):
        source_sha = hashlib.sha256(Path(self.original["artifact"]).read_bytes()).hexdigest()
        legacy = (self.root / "memory/learning_goal_evidence" /
                  f"{self.goal_id}_{source_sha}_official_relevant_v2.json")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text('{"old": true}\n', encoding="utf-8")
        result = replay_official_learning_quotes(self.root, self.goal_id)
        self.assertTrue(result["artifact"].endswith("_official_relevant_v3.json"))
        self.assertEqual(legacy.read_text(encoding="utf-8"), '{"old": true}\n')

    def test_short_intro_does_not_extend_quote_into_example_code(self):
        self.pages["docs.pytest.org"] = (
            '<main><p>pytest also provides a number of utilities to make writing tests easier.</p>'
            '<p>For example, you can use pytest.approx() to compare floating-point values '
            'that may have small rounding errors:</p>'
            '<pre># content of test_approx.py\nimport pytest\ndef test_sum():\n'
            '    assert (0.1 + 0.2) == pytest .\nnext output line</pre></main>'
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            goal_id = LearningGoalStore(root).create("Python software testing", public_research_allowed=True)["goal_id"]

            def fetch(url):
                return url, "text/html", self.pages[urlsplit(url).hostname].encode("utf-8")

            collect_official_learning_sources(root, goal_id, fetcher=fetch)
            report = replay_official_learning_quotes(root, goal_id)
            pytest_quotes = [q["quote"] for q in report["quotes"] if q["source_id"] == "S2"]
            self.assertIn("pytest also provides a number of utilities to make writing tests easier.", pytest_quotes)
            self.assertFalse(any("# content" in quote or "import pytest" in quote for quote in pytest_quotes))


if __name__ == "__main__":
    unittest.main()
