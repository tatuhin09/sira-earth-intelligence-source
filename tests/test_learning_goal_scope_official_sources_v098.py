"""Scope and authorization study retains two official pages as evidence."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goal_general_learning import collect_and_verify_goal_sources
from sira.learning_goals import LearningGoalStore


FOCUS = "Penetration testing scope and authorization"
OWASP = "https://cheatsheetseries.owasp.org/cheatsheets/Vulnerability_Disclosure_Cheat_Sheet.html"
CISA = ("https://www.cisa.gov/news-events/news/"
        "cisa-issues-final-vulnerability-disclosure-policy-directive-federal-agencies")
WIKI = "https://en.wikipedia.org/wiki/Security_testing"
TEXTS = {
    OWASP: ("For authorized vulnerability testing under a program, researchers should "
            "read and stay within the scope and rules of that program. This defines limits."),
    CISA: ("A published vulnerability disclosure policy explains what types of testing "
           "are authorized for which systems and where to report vulnerabilities."),
    WIKI: "Security testing evaluates whether applications protect their resources correctly.",
}


class ScopeOfficialSourcesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = LearningGoalStore(self.root)
        row = self.store.create(
            "Ethical hacking and authorized penetration testing",
            public_research_allowed=True,
        )
        self.goal = self.store.set_plan(row["goal_id"], [FOCUS, "Authorized reconnaissance and asset inventory"])
        self.research = {
            "knowledge": {"results": [{"title": "Security testing", "url": WIKI}]},
            "open_access": {"results": []},
        }

    def fetch(self, url):
        return url, "text/html", ("<main><p>" + TEXTS[url] + "</p></main>").encode()

    def test_two_official_hosts_are_saved_without_false_verification(self):
        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=self.fetch, focus=FOCUS,
        )
        self.assertEqual([row["url"] for row in result["documents"]], [OWASP, CISA])
        self.assertEqual(result["status"], "unverified_source_statements")
        self.assertEqual((result["api_requests"], result["metered_model_requests"]), (2, 0))
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertTrue(Path(result["artifact"]).is_file())
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_permission_revoked_after_first_page_blocks_second(self):
        requests = []

        def fetch(url):
            requests.append(url)
            self.store.set_policy(self.goal["goal_id"], public_research_allowed=False)
            return self.fetch(url)

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=fetch, focus=FOCUS,
        )
        self.assertEqual(requests, [OWASP])
        self.assertEqual(result["status"], "research_permission_revoked")
        self.assertNotIn("artifact", result)

    def test_redirected_second_page_remains_unverified(self):
        requests = []

        def fetch(url):
            requests.append(url)
            if url == CISA:
                return "https://example.com/redirect", "text/html", b"<main>Unexpected host</main>"
            return self.fetch(url)

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=fetch, focus=FOCUS,
        )
        self.assertEqual(requests, [OWASP, CISA])
        self.assertEqual(result["status"], "independent_source_unavailable")
        self.assertEqual([row["host"] for row in result["documents"]],
                         ["cheatsheetseries.owasp.org"])
        self.assertEqual(result["verified_claim_count"], 0)


if __name__ == "__main__":
    unittest.main()
