"""Autonomous study can use narrow official corroboration without model calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.desktop_app import DesktopControl
from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goal_general_learning import collect_and_verify_goal_sources
from sira.learning_goal_curated_runtime import verify_curated_saved_claim
from sira.learning_goal_runtime import run_learning_goal_target
from sira.learning_goals import LearningGoalStore
from sira.runtime import RuntimeStateStore, run_unified_improvement_cycle


OWASP = "https://cheatsheetseries.owasp.org/cheatsheets/Vulnerability_Disclosure_Cheat_Sheet.html"
CISA = ("https://www.cisa.gov/news-events/news/"
        "cisa-issues-final-vulnerability-disclosure-policy-directive-federal-agencies")
SCOPE_TEXT = {
    OWASP: ("If you are carrying out testing under a bug bounty or similar program, "
            "the organization may have established safe harbor policies, that allow "
            "you to legally carry out testing, as long as you stay within the scope "
            "and rules of their program . Read the scope carefully."),
    CISA: ("Policies make it easier for the public to know where to send a report, "
           "what types of testing are authorized for which systems, and what "
           "communication to expect."),
}
RISK_FOCUS = "Cybersecurity risk assessment and threat modeling"
RISK_URLS = (
    "https://www.cisa.gov/resources-tools/resources/risk-assessment-methodologies",
    "https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html",
)
RISK_TEXT = {
    RISK_URLS[0]: ("Risk assessment involves the evaluation of risks taking into "
                   "consideration the potential direct and indirect consequences "
                   "of an incident, known vulnerabilities to various hazards."),
    RISK_URLS[1]: ("Threat modeling seeks to identify potential security issues "
                   "during the design phase. The team can then plan defenses."),
}


class CuratedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create(
            "Ethical hacking and authorized penetration testing",
            public_research_allowed=True,
        )
        self.focus = "Penetration testing scope and authorization"
        self.store.set_plan(self.goal["goal_id"], [self.focus, "Vulnerability validation"])
        self.research = {"knowledge": {"results": []}, "open_access": {"results": []}}

    def fetch(self, url):
        return url, "text/html", ("<main><p>" + SCOPE_TEXT[url] + "</p></main>").encode()

    def collect(self):
        return collect_and_verify_goal_sources(
            self.root, self.goal["goal_id"], self.goal["topic"],
            self.research, fetcher=self.fetch, focus=self.focus,
        )

    def test_scope_claim_is_consolidated_during_source_collection(self):
        report = self.collect()
        self.assertEqual(report["status"], "verified_knowledge_recorded")
        self.assertEqual(report["verified_claim_count"], 1)
        self.assertEqual(len(report["claims"]), 1)
        self.assertEqual(set(report["claims"][0]["source_urls"]), {OWASP, CISA})
        self.assertEqual(report["api_requests"], 2)
        self.assertEqual(report["metered_model_requests"], 0)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 1)

    def test_identical_pages_on_later_cycle_do_not_inflate_evidence(self):
        self.collect()
        self.collect()
        stats = KnowledgeConsolidationStore(self.root).stats()
        self.assertEqual(stats["active_knowledge"], 1)
        self.assertEqual(stats["knowledge_evidence"], 2)

    def test_collected_claim_is_attributed_without_fabricating_attempt(self):
        self.collect()
        with patch("sira.desktop_app.run_desktop_model_chat",
                   side_effect=AssertionError("Model must not run")):
            detail = DesktopControl(self.root).chat_send("Goal show " + self.goal["goal_id"])
        self.assertIn("Progress: 1 of 2 steps", detail["assistant"]["text"])
        self.assertEqual(self.store.get(self.goal["goal_id"])["study_step_stats"][0]["attempt_count"], 0)

    def test_missing_clause_is_kept_unverified(self):
        original = SCOPE_TEXT[CISA]
        try:
            SCOPE_TEXT[CISA] = "A vulnerability disclosure policy can list reporting contacts. " * 3
            result = self.collect()
        finally:
            SCOPE_TEXT[CISA] = original
        self.assertEqual(result["status"], "unverified_source_statements")
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_invalid_saved_host_fails_closed_without_type_error(self):
        original = SCOPE_TEXT[CISA]
        try:
            SCOPE_TEXT[CISA] = "A vulnerability disclosure policy can list reporting contacts. " * 3
            result = self.collect()
        finally:
            SCOPE_TEXT[CISA] = original
        path = Path(result["artifact"])
        saved = json.loads(path.read_text())
        saved["documents"][0]["host"] = []
        path.write_text(json.dumps(saved), encoding="utf-8")
        report = verify_curated_saved_claim(
            self.root, self.goal["goal_id"], self.goal["topic"], self.focus, path,
        )
        self.assertEqual(report["status"], "unverified_source_statements")

    def test_other_curated_risk_focus_also_verifies(self):
        row = self.store.create("Cybersecurity fundamentals and defense",
                                public_research_allowed=True)
        self.store.set_plan(row["goal_id"], [RISK_FOCUS, "Network segmentation"])
        result = collect_and_verify_goal_sources(
            self.root, row["goal_id"], row["topic"], self.research,
            fetcher=lambda url: (url, "text/html", ("<main><p>" + RISK_TEXT[url] + "</p></main>").encode()),
            focus=RISK_FOCUS,
        )
        self.assertEqual(result["status"], "verified_knowledge_recorded")
        self.assertEqual(result["verified_claim_count"], 1)

    def test_learning_cycle_updates_goal_step_and_chat_without_model(self):
        target = {"target_kind": "learning_goal", "learning_goal_id": self.goal["goal_id"],
                  "topic": self.goal["topic"]}
        search = lambda *_a, **_k: {
            "results": [{"title": self.focus, "url": OWASP}],
            "metrics": {"api_requests": 1},
        }
        report = run_learning_goal_target(
            self.root, target, now_epoch=2_000_000_000.0,
            knowledge_searcher=search, open_access_searcher=search,
            document_fetcher=self.fetch,
        )
        self.assertEqual(report["outcome"], "verified_knowledge_recorded")
        self.assertEqual(report["verified_claim_count"], 1)
        self.assertEqual(report["metered_model_requests"], 0)
        self.assertFalse(report["main_tree_modified"])
        goal = self.store.get(self.goal["goal_id"])
        self.assertEqual(goal["study_step_stats"][0]["verified_claim_events"], 1)
        with patch("sira.desktop_app.run_desktop_model_chat",
                   side_effect=AssertionError("Model must not run")):
            detail = DesktopControl(self.root).chat_send("Goal show " + self.goal["goal_id"])
        self.assertIn("Progress: 1 of 2 steps", detail["assistant"]["text"])

    def test_official_sources_work_when_public_search_returns_no_metadata(self):
        target = {"target_kind": "learning_goal", "learning_goal_id": self.goal["goal_id"],
                  "topic": self.goal["topic"]}
        empty = lambda *_a, **_k: {"results": [], "metrics": {"api_requests": 0}}
        report = run_learning_goal_target(
            self.root, target, now_epoch=2_000_000_000.0,
            knowledge_searcher=empty, open_access_searcher=empty,
            document_fetcher=self.fetch,
        )
        self.assertEqual(report["outcome"], "verified_knowledge_recorded")
        self.assertEqual(report["api_requests"], 2)
        self.assertEqual(report["metered_model_requests"], 0)

    def test_permission_revoked_after_first_evidence_stays_unverified(self):
        original = KnowledgeConsolidationStore.record_evidence
        calls = []

        def record(instance, *args, **kwargs):
            recorded = original(instance, *args, **kwargs)
            calls.append(recorded)
            if len(calls) == 1:
                self.store.set_policy(self.goal["goal_id"], public_research_allowed=False)
            return recorded

        with patch.object(KnowledgeConsolidationStore, "record_evidence", record):
            report = self.collect()
        self.assertEqual(report["status"], "research_permission_revoked")
        self.assertEqual(report["verified_claim_count"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(KnowledgeConsolidationStore(self.root).stats()["active_knowledge"], 0)

    def test_unified_cycle_records_claim_with_runtime_on_and_finishes_off(self):
        target = {"target_kind": "learning_goal", "learning_goal_id": self.goal["goal_id"],
                  "topic": self.goal["topic"]}
        state = RuntimeStateStore(self.root)
        state.save({
            "desired_state": "on", "worker_state": "starting", "pid": os.getpid(),
            "started_at": "2026-09-27T10:00:00+00:00",
            "heartbeat_at": "2026-09-27T10:00:00+00:00", "generation": 63,
        })
        empty = lambda *_a, **_k: {"results": [], "metrics": {"api_requests": 0}}
        cycle = run_unified_improvement_cycle(
            self.root, 63,
            target_selector=lambda *_: {
                "status": "selected", "selection_id": "ats_" + "1" * 32,
                "target": target, "alternatives": [],
            },
            learning_goal_runner=lambda root, selected: run_learning_goal_target(
                root, selected, now_epoch=2_000_000_000.0,
                knowledge_searcher=empty, open_access_searcher=empty,
                document_fetcher=self.fetch,
            ),
        )
        self.assertEqual(cycle["outcome"], "verified_knowledge_recorded")
        self.assertFalse(cycle["promotion_performed"])
        self.assertFalse(cycle["main_tree_modified"])
        self.assertEqual(cycle["resource_usage"]["metered_model_requests"], 0)
        self.assertEqual(self.store.get(self.goal["goal_id"])["study_step_stats"][0]["verified_claim_events"], 1)
        self.assertEqual(state.status()["desired_state"], "off")


if __name__ == "__main__":
    unittest.main()
