"""A74 practice runs are fixed local exercises, bounded and never skill evidence."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_consolidation import LearningConsolidationStore
from sira.cli import main
from sira.learning_goal_practice import execute_practice_candidate
from sira.learning_goal_security_claim import record_security_risk_claim
from sira.learning_goal_skill_candidates import advisory_skill_candidates
from sira.learning_goals import LearningGoalStore


class PracticeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create("Cybersecurity fundamentals and defense",
                                      public_research_allowed=True)
        focus = "Cybersecurity risk assessment and threat modeling"
        self.store.set_plan(self.goal["goal_id"],
                            [focus, "Network security controls and segmentation"])
        self.artifact = (self.root / "memory/learning_goal_documents" /
                         f"{self.goal['goal_id']}_{'a' * 32}.json")
        self.artifact.parent.mkdir(parents=True, exist_ok=True)
        documents = [
            ("https://www.cisa.gov/resources-tools/resources/risk-assessment-methodologies",
             "www.cisa.gov", "Risk Assessment Methodologies",
             "Risk assessment involves the evaluation of risks taking into consideration "
             "the potential direct and indirect consequences of an incident, "
             "known vulnerabilities to various potential hazards."),
            ("https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html",
             "cheatsheetseries.owasp.org", "Threat Modeling Cheat Sheet",
             "Threat modeling seeks to identify potential security issues during the "
             "design phase. It helps teams assess their security design."),
        ]
        self.artifact.write_text(json.dumps({
            "schema": "sira.learning_goal_general_documents.v1",
            "learning_goal_id": self.goal["goal_id"], "topic": self.goal["topic"],
            "research_query": focus, "status": "quotes_need_claim_verification",
            "documents": [
                {"url": url, "host": host, "title": title, "text": text,
                 "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                 "retrieved_at": "2026-09-27T10:00:00+00:00", "verified": False}
                for url, host, title, text in documents
            ], "metadata_evidence": [], "source_failures": [],
            "verified_claims_recorded": False,
        }), encoding="utf-8")
        digest = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        record_security_risk_claim(self.root, self.goal["goal_id"], self.artifact,
                                   expected_artifact_sha256=digest)
        self.candidate = advisory_skill_candidates(
            self.root, self.store.get(self.goal["goal_id"]))[0]

    def run_candidate(self, *, when=2_000_000_000.0):
        return execute_practice_candidate(
            self.root, self.goal["goal_id"], self.candidate["candidate_id"],
            now_epoch=when,
        )

    def test_fixed_exercise_runs_in_private_directory_and_records_result(self):
        report = self.run_candidate()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["expected_capability"],
                         "research.trace_verified_source_provenance.v1")
        self.assertEqual(report["execution_result"]["source_hosts"],
                         ["cheatsheetseries.owasp.org", "www.cisa.gov"])
        self.assertEqual(report["failure_reason"], None)
        self.assertFalse(report["skill_activated"])
        self.assertFalse(report["promotion_performed"])
        self.assertFalse(report["authority_granted"])
        self.assertEqual(report["resource_usage"]["paid_requests"], 0)
        self.assertEqual(report["resource_usage"]["api_requests"], 0)
        self.assertLessEqual(report["resource_usage"]["wall_seconds"], 5)
        for artifact in report["artifacts"].values():
            path = Path(artifact)
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        saved = json.loads(Path(report["artifacts"]["record"]).read_text())
        self.assertEqual(saved, report)
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])

    def test_tampered_or_paused_candidate_fails_before_child(self):
        saved = json.loads(self.artifact.read_text())
        saved["documents"][0]["text"] += " tampered"
        self.artifact.write_text(json.dumps(saved), encoding="utf-8")
        with patch("sira.learning_goal_practice.subprocess.run",
                   side_effect=AssertionError("Child must not run")):
            report = self.run_candidate()
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["failure_reason"], "candidate_not_verified")
        self.assertIsNone(report["execution_result"])
        self.assertFalse(report["skill_activated"])

        # The same verified artifact cannot override a later owner pause.
        self.store.set_status(self.goal["goal_id"], "paused")
        with patch("sira.learning_goal_practice.subprocess.run",
                   side_effect=AssertionError("Child must not run")):
            paused = self.run_candidate(when=2_000_001_800.0)
        self.assertEqual(paused["status"], "rejected")
        self.assertEqual(paused["failure_reason"], "candidate_not_verified")

    def test_timeout_and_result_mismatch_fail_closed_with_bounded_retries(self):
        with patch("sira.learning_goal_practice.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(["fixed-worker"], 3)):
            first = self.run_candidate()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["failure_reason"], "practice_timeout")
        self.assertIsNone(first["resource_usage"]["cpu_seconds"])
        self.assertIsNone(first["resource_usage"]["peak_rss_kib"])
        self.assertFalse(first["skill_activated"])
        blocked = self.run_candidate(when=2_000_000_100.0)
        self.assertEqual(blocked["status"], "deferred")
        self.assertEqual(blocked["failure_reason"], "practice_cooldown")
        self.assertEqual(self.run_candidate(when=2_000_001_800.0)["status"], "completed")
        self.assertEqual(self.run_candidate(when=2_000_003_600.0)["status"], "completed")
        with patch("sira.learning_goal_practice.subprocess.run",
                   side_effect=AssertionError("Attempt limit must not start a child")):
            limited = self.run_candidate(when=2_000_005_400.0)
        self.assertEqual(limited["status"], "deferred")
        self.assertEqual(limited["failure_reason"], "practice_attempt_limit")
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])

    def test_unverified_candidate_id_and_symlink_destination_do_not_execute(self):
        with patch("sira.learning_goal_practice.subprocess.run",
                   side_effect=AssertionError("Child must not run")):
            result = execute_practice_candidate(self.root, self.goal["goal_id"],
                                                "lsc_" + "f" * 24)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["failure_reason"], "candidate_not_verified")

        root = self.root / "memory/learning_practice_runs"
        self.assertTrue(root.is_dir())
        # A replaced destination must not be followed into another directory.
        root.rename(root.with_name("learning_practice_runs_old"))
        root.symlink_to(self.root)
        with self.assertRaises(ValueError):
            execute_practice_candidate(self.root, self.goal["goal_id"],
                                       self.candidate["candidate_id"])

    def test_cli_lists_and_executes_only_the_fixed_local_exercise(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--root", str(self.root), "goals", "practice-list",
                         self.goal["goal_id"]])
        self.assertEqual(code, 0)
        listed = json.loads(output.getvalue())
        self.assertEqual(listed["candidates"][0]["candidate_id"],
                         self.candidate["candidate_id"])
        self.assertTrue(listed["candidates"][0]["executable"])
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--root", str(self.root), "goals", "practice",
                         self.goal["goal_id"], self.candidate["candidate_id"]])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "completed")
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])

    def test_worker_output_mismatch_is_recorded_as_failure(self):
        def mismatched(*_args, **kwargs):
            (Path(kwargs["cwd"]) / "result.json").write_text(
                json.dumps({"candidate_id": "wrong"}), encoding="utf-8")
            return subprocess.CompletedProcess(["fixed-worker"], 0)

        with patch("sira.learning_goal_practice.subprocess.run", side_effect=mismatched):
            report = self.run_candidate()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failure_reason"], "practice_result_mismatch")
        self.assertFalse(report["skill_evidence_recorded"])
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])

    def test_source_changes_while_worker_runs_are_rejected(self):
        real_run = subprocess.run

        def run_then_change(*args, **kwargs):
            completed = real_run(*args, **kwargs)
            saved = json.loads(self.artifact.read_text(encoding="utf-8"))
            saved["documents"][0]["text"] += " altered during practice"
            self.artifact.write_text(json.dumps(saved), encoding="utf-8")
            return completed

        with patch("sira.learning_goal_practice.subprocess.run", side_effect=run_then_change):
            report = self.run_candidate()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failure_reason"], "candidate_evidence_changed")
        self.assertIsNone(report["execution_result"])
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])


if __name__ == "__main__":
    unittest.main()
