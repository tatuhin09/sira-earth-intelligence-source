"""Broad owner study topics can receive bounded, unverified search hints."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_goal_followup import title_followup_hint


class PartialTitleFollowupTests(unittest.TestCase):
    def test_two_independent_titles_with_two_focus_terms_can_hint(self):
        focus = "Cybersecurity risk assessment and threat modeling"
        research = {
            "knowledge": {"results": [{
                "title": "Cybersecurity risk assessment frameworks",
                "url": "https://en.wikipedia.org/wiki/Risk_assessment",
            }]},
            "open_access": {"results": [{
                "title": "Risk assessment frameworks for threat analysis",
                "url": "https://doaj.org/article/abc",
            }]},
        }
        self.assertEqual(title_followup_hint(focus, research), {
            "term": "frameworks",
            "source_hosts": ["doaj.org", "en.wikipedia.org"],
            "source_urls": ["https://doaj.org/article/abc",
                            "https://en.wikipedia.org/wiki/Risk_assessment"],
        })

    def test_one_focus_term_or_one_host_is_not_enough(self):
        focus = "Cybersecurity risk assessment and threat modeling"
        knowledge = {"title": "Cybersecurity risk assessment frameworks",
                     "url": "https://en.wikipedia.org/wiki/Risk_assessment"}
        unrelated = {"title": "Education assessment frameworks",
                     "url": "https://doaj.org/article/abc"}
        same_host = {"title": "Risk assessment frameworks for threat analysis",
                     "url": "https://en.wikipedia.org/wiki/Another_assessment"}
        for second in (unrelated, same_host):
            with self.subTest(second=second):
                research = {"knowledge": {"results": [knowledge]},
                            "open_access": {"results": [second]}}
                self.assertIsNone(title_followup_hint(focus, research))

    def test_stronger_shared_focus_match_wins_over_alphabetical_term(self):
        focus = "Cybersecurity risk assessment and threat modeling"
        research = {
            "knowledge": {"results": [
                {"title": "Risk assessment adaptive approach",
                 "url": "https://en.wikipedia.org/wiki/Adaptive"},
                {"title": "Cybersecurity risk assessment frameworks",
                 "url": "https://en.wikipedia.org/wiki/Framework"},
            ]},
            "open_access": {"results": [
                {"title": "Threat assessment adaptive approach", "url": "https://doaj.org/article/a"},
                {"title": "Risk assessment threat frameworks",
                 "url": "https://doaj.org/article/b"},
            ]},
        }
        hint = title_followup_hint(focus, research)
        self.assertIsNotNone(hint)
        self.assertEqual(hint["term"], "frameworks")


if __name__ == "__main__":
    unittest.main()
