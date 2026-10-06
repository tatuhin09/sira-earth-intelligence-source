"""Readable official pages can replace blocked paper hosts as unverified evidence."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goal_general_learning import collect_and_verify_goal_sources
from sira.learning_goals import LearningGoalStore


FOCUS = "Cybersecurity risk assessment and threat modeling"
CISA = "https://www.cisa.gov/resources-tools/resources/risk-assessment-methodologies"
OWASP = "https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html"
DOAJ = "https://doaj.org/article/03cb888d494a4dd3bf0bc1ee6e7fadb9"
WIKI = "https://en.wikipedia.org/wiki/Cybersecurity_Maturity_Model_Certification"
ARXIV = "https://arxiv.org/abs/2601.01234"
TEXTS = {
    CISA: "Cybersecurity risk assessment helps public safety organizations evaluate risks to their operations.",
    OWASP: "Threat modeling helps teams identify and rank applicable threats to a system during design.",
    DOAJ: "A cybersecurity threat assessment models risks to industrial control systems in this study.",
    WIKI: "Cybersecurity maturity programs assess defensive controls across different organizations.",
    ARXIV: "Cybersecurity risk assessment and threat modeling identify weaknesses in computer systems.",
}


class OfficialFallbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = LearningGoalStore(self.root)
        goal = self.store.create("Cybersecurity fundamentals and defense", public_research_allowed=True)
        self.goal = self.store.set_plan(goal["goal_id"], [FOCUS, "Network security controls and segmentation"])
        self.research = {
            "knowledge": {"results": [{"title": "Cybersecurity Maturity Model Certification", "url": WIKI}]},
            "open_access": {"results": [{
                "title": "Legacy ICS Cybersecurity Assessment Using Hybrid Threat Modeling",
                "url": DOAJ, "provider": "doaj", "abstract": "Relevant but inaccessible article abstract.",
            }]},
        }

    def fetch(self, url):
        return url, "text/html", ("<html><main><p>" + TEXTS[url] + "</p></main></html>").encode()

    def test_two_readable_official_hosts_are_stored_without_false_claim(self):
        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=self.fetch, focus=FOCUS,
        )
        self.assertEqual([row["url"] for row in result["documents"]], [CISA, OWASP])
        self.assertEqual(result["status"], "unverified_source_statements")
        self.assertEqual(result["api_requests"], 2)
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_revocation_after_first_official_page_stops_next_request(self):
        requests = []

        def fetch(url):
            requests.append(url)
            self.store.set_policy(self.goal["goal_id"], public_research_allowed=False)
            return self.fetch(url)

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=fetch, focus=FOCUS,
        )
        self.assertEqual(requests, [CISA])
        self.assertEqual(result["status"], "research_permission_revoked")
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_redirected_official_page_does_not_retry_blocked_article(self):
        requests = []

        def fetch(url):
            requests.append(url)
            if url == OWASP:
                return "https://community.owasp.org/Threat_Modeling", "text/html", b"<main>redirected</main>"
            if url == DOAJ:
                raise AssertionError("Blocked paper article must not be requested in this fallback")
            return self.fetch(url)

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=fetch, focus=FOCUS,
        )
        self.assertEqual(requests, [CISA, OWASP])
        self.assertEqual(result["status"], "independent_source_unavailable")
        self.assertEqual(result["api_requests"], 2)
        self.assertEqual([row["host"] for row in result["documents"]], ["www.cisa.gov"])
        self.assertEqual(result["verified_claim_count"], 0)

    def test_relevant_arxiv_remains_available_after_official_failure(self):
        self.research["open_access"]["results"].append({
            "title": "Cybersecurity risk assessment through threat modeling",
            "url": ARXIV, "provider": "arxiv", "abstract": TEXTS[ARXIV],
        })
        requests = []

        def fetch(url):
            requests.append(url)
            if url == OWASP:
                return "https://community.owasp.org/Threat_Modeling", "text/html", b"<main>redirected</main>"
            return self.fetch(url)

        result = collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=fetch, focus=FOCUS,
        )
        self.assertEqual(requests, [CISA, OWASP, ARXIV])
        self.assertEqual([row["host"] for row in result["documents"]],
                         ["www.cisa.gov", "arxiv.org"])
        self.assertEqual(result["verified_claim_count"], 0)

    def test_official_urls_do_not_override_other_subjects(self):
        other = self.store.create("Python software testing", public_research_allowed=True)
        result = collect_and_verify_goal_sources(
            self.root, other["goal_id"], other["topic"], {
                "knowledge": {"results": [{"title": "Python software testing", "url": WIKI}]},
                "open_access": {"results": [{"title": "Python software testing", "url": DOAJ}]},
            }, fetcher=self.fetch,
        )
        self.assertEqual({row["url"] for row in result["documents"]}, {WIKI, DOAJ})


if __name__ == "__main__":
    unittest.main()
