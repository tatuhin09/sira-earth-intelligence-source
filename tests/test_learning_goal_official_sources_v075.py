"""Bounded documentation fetching yields exact quotes, never learned facts."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_goal_official_sources import collect_official_learning_sources
from sira.learning_goals import LearningGoalStore


class OfficialLearningSourcesV075Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.goals = LearningGoalStore(self.root)
        self.goal = self.goals.create("Python software testing", public_research_allowed=True)
        self.pages = {
            "https://docs.python.org/3/library/unittest.html":
                b'<html><nav>Noise</nav><div role="main"><h1>unittest</h1><p>Python unittest supports automated software testing and test cases.</p><script>secret</script></div></html>',
            "https://docs.pytest.org/en/stable/getting-started.html":
                b'<html><main><h1>pytest</h1><p>Python testing with pytest uses assert statements to verify expectations.</p></main></html>',
        }
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self, url):
        self.calls.append(url)
        return url, "text/html", self.pages[url]

    def test_two_fixed_official_pages_yield_replayable_unverified_quotes(self):
        report = collect_official_learning_sources(self.root, self.goal["goal_id"], fetcher=self.fetch)
        self.assertEqual(report["status"], "quotes_need_claim_verification")
        self.assertEqual(report["api_requests"], 2)
        self.assertEqual(report["source_count"], 2)
        self.assertEqual(report["distinct_host_count"], 2)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(report["verified_claims_recorded"])
        self.assertFalse(report["promotion_performed"])
        for source in report["sources"]:
            self.assertEqual(source["raw_sha256"], hashlib.sha256(self.pages[source["url"]]).hexdigest())
            self.assertNotIn("Noise", source["text"])
            self.assertNotIn("secret", source["text"])
            for quote in source["quotes"]:
                self.assertEqual(source["text"][quote["start"]:quote["end"]], quote["quote"])
                self.assertFalse(quote["verified"])
        self.assertTrue(Path(report["artifact"]).is_file())
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())
        cached = collect_official_learning_sources(self.root, self.goal["goal_id"], fetcher=self.fetch)
        self.assertEqual(cached["api_requests"], 0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(cached["sources"], report["sources"])

    def test_redirect_and_non_html_fail_closed_without_a_saved_claim(self):
        wrong = lambda url: ("https://other.example.org/redirect", "text/html", b"<main>Python software testing</main>")
        with self.assertRaises(ValueError):
            collect_official_learning_sources(self.root, self.goal["goal_id"], fetcher=wrong)
        self.assertFalse((self.root / "memory/learning_goal_documents").exists())
        wrong_type = lambda url: (url, "application/pdf", b"%PDF")
        with self.assertRaises(ValueError):
            collect_official_learning_sources(self.root, self.goal["goal_id"], fetcher=wrong_type)
        self.assertFalse((self.root / "memory/sira_knowledge.sqlite3").exists())

    def test_permission_is_checked_again_before_each_request(self):
        def revoke(url):
            self.goals.set_policy(self.goal["goal_id"], public_research_allowed=False)
            return self.fetch(url)
        with self.assertRaises(ValueError):
            collect_official_learning_sources(self.root, self.goal["goal_id"], fetcher=revoke)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.root / "memory/learning_goal_documents").exists())

    def test_permission_revoked_during_last_request_prevents_saving_evidence(self):
        def revoke_last(url):
            result = self.fetch(url)
            if len(self.calls) == 2:
                self.goals.set_policy(self.goal["goal_id"], public_research_allowed=False)
            return result
        with self.assertRaises(ValueError):
            collect_official_learning_sources(self.root, self.goal["goal_id"], fetcher=revoke_last)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.root / "memory/learning_goal_documents").exists())

    def test_corrupt_cache_is_not_treated_as_valid_evidence(self):
        result = collect_official_learning_sources(self.root, self.goal["goal_id"], fetcher=self.fetch)
        path = Path(result["artifact"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["sources"][0]["text"] = "A fabricated claim"
        path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaises(ValueError):
            collect_official_learning_sources(self.root, self.goal["goal_id"], fetcher=self.fetch)
        self.assertEqual(len(self.calls), 2)

    def test_cache_cannot_upgrade_a_source_or_the_overall_status(self):
        result = collect_official_learning_sources(self.root, self.goal["goal_id"], fetcher=self.fetch)
        path = Path(result["artifact"])
        original = json.loads(path.read_text(encoding="utf-8"))
        for field in ("source_verified", "status_verified"):
            saved = json.loads(json.dumps(original))
            if field == "source_verified":
                saved["sources"][0]["verified"] = True
            else:
                saved["status"] = "verified"
            path.write_text(json.dumps(saved), encoding="utf-8")
            with self.subTest(field=field), self.assertRaises(ValueError):
                collect_official_learning_sources(self.root, self.goal["goal_id"], fetcher=self.fetch)
        self.assertEqual(len(self.calls), 2)

    def test_other_topics_do_not_receive_python_specific_sources(self):
        another = self.goals.create("Soil science", public_research_allowed=True)
        with self.assertRaises(ValueError):
            collect_official_learning_sources(self.root, another["goal_id"], fetcher=self.fetch)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
