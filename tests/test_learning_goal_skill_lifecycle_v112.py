"""A76: independently checked runs are bounded lifecycle evidence, not mastery."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_consolidation import LearningConsolidationStore
from sira.learning_goal_practice import execute_practice_candidate
from sira.learning_goal_practice_verification import verify_practice_run
from sira.learning_goal_skill_lifecycle import (
    get_skill_lifecycle, list_active_practice_skills, reconcile_skill_lifecycle,
)
from tests import test_learning_goal_practice_verification_v111 as a75_fixture


class VerifiedSkillLifecycleTests(unittest.TestCase):
    def setUp(self):
        a75_fixture.IndependentPracticeVerificationTests.setUp(self)
        self.goal_id = self.goal["goal_id"]
        self.candidate_id = self.candidate["candidate_id"]

    def reconcile(self, **kwargs):
        return reconcile_skill_lifecycle(self.root, self.goal_id, self.candidate_id,
                                         **kwargs)

    def extra_run(self):
        with patch("sira.learning_goal_practice.RETRY_SECONDS", 0):
            run = execute_practice_candidate(self.root, self.goal_id, self.candidate_id,
                                             now_epoch=time.time() + 5)
        self.assertEqual(run["status"], "completed")
        decision = verify_practice_run(self.root, self.goal_id, self.candidate_id,
                                       run["run_id"])
        self.assertEqual(decision["status"], "verified")
        return run, decision

    def test_one_independent_success_only_gives_pending_evidence(self):
        decision = verify_practice_run(self.root, self.goal_id, self.candidate_id,
                                       self.run["run_id"])
        self.assertEqual(decision["status"], "verified")
        state = self.reconcile()
        self.assertEqual(state["state"], "independently_verified")
        self.assertFalse(state["active"])
        self.assertEqual(state["success_count"], 1)
        self.assertEqual(state["required_successes"], 2)
        self.assertEqual(state["reason"], "more_independent_successes_required")
        self.assertEqual(list_active_practice_skills(self.root, self.goal_id), [])

    def test_distinct_successes_activate_only_demonstrated_capability(self):
        first = verify_practice_run(self.root, self.goal_id, self.candidate_id,
                                    self.run["run_id"])
        second_run, second = self.extra_run()
        state = self.reconcile()
        self.assertEqual(state["state"], "active")
        self.assertTrue(state["active"])
        self.assertEqual(state["success_count"], 2)
        self.assertEqual(state["scope"], "research.trace_verified_source_provenance.v1")
        self.assertFalse(state["domain_mastery_certified"])
        self.assertEqual(state["evidence_strength"], "two_distinct_independent_runs")
        self.assertEqual({e["run_id"] for e in state["evidence"]},
                         {self.run["run_id"], second_run["run_id"]})
        self.assertEqual({e["verification_fingerprint"] for e in state["evidence"]},
                         {first["reproducibility"]["decision_fingerprint"],
                          second["reproducibility"]["decision_fingerprint"]})
        self.assertTrue(all(e["knowledge_key"] == self.candidate["knowledge_key"]
                            for e in state["evidence"]))
        self.assertEqual(len(list_active_practice_skills(self.root, self.goal_id)), 1)
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])

    def test_duplicate_or_replayed_verification_does_not_count_twice(self):
        decision = verify_practice_run(self.root, self.goal_id, self.candidate_id,
                                       self.run["run_id"])
        path = Path(decision["verifier_artifact"])
        fake_run_id = "lpr_" + "f" * 32
        (path.parent / (fake_run_id + ".json")).write_bytes(path.read_bytes())
        state = self.reconcile()
        self.assertFalse(state["active"])
        self.assertEqual(state["success_count"], 1)
        self.assertEqual(state["rejected_count"], 1)

    def test_failed_ambiguous_and_self_certifying_execution_cannot_count(self):
        rejected = execute_practice_candidate(self.root, self.goal_id,
                                              "lsc_" + "f" * 24)
        verify_practice_run(self.root, self.goal_id, rejected["candidate_id"],
                            rejected["run_id"])
        self.assertEqual(self.reconcile()["state"], "practicing")
        record = Path(self.run["artifacts"]["record"])
        value = json.loads(record.read_text())
        value["skill_activated"] = True
        record.write_text(json.dumps(value))
        decision = verify_practice_run(self.root, self.goal_id, self.candidate_id,
                                       self.run["run_id"])
        self.assertEqual(decision["status"], "rejected")
        state = self.reconcile()
        self.assertFalse(state["active"])
        self.assertEqual(state["success_count"], 0)
        self.assertEqual(state["state"], "failed")

    def test_tampered_verifier_or_mismatched_provenance_does_not_count(self):
        decision = verify_practice_run(self.root, self.goal_id, self.candidate_id,
                                       self.run["run_id"])
        path = Path(decision["verifier_artifact"])
        value = json.loads(path.read_text())
        value["provenance"]["knowledge_key"] = "other"
        path.write_text(json.dumps(value))
        state = self.reconcile()
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["success_count"], 0)
        self.assertEqual(state["rejected_count"], 1)

    def test_malformed_verifier_evidence_fails_closed(self):
        decision = verify_practice_run(self.root, self.goal_id, self.candidate_id,
                                       self.run["run_id"])
        path = Path(decision["verifier_artifact"])
        value = json.loads(path.read_text())
        value["verifier_evidence"] = []
        path.write_text(json.dumps(value))
        state = self.reconcile()
        self.assertFalse(state["active"])
        self.assertEqual(state["state"], "failed")

    def test_knowledge_source_tamper_deprecates_active_skill(self):
        verify_practice_run(self.root, self.goal_id, self.candidate_id,
                            self.run["run_id"])
        self.extra_run()
        self.assertTrue(self.reconcile()["active"])
        source = json.loads(self.artifact.read_text())
        source["documents"][0]["text"] += " altered"
        self.artifact.write_text(json.dumps(source))
        state = get_skill_lifecycle(self.root, self.goal_id, self.candidate_id)
        self.assertEqual(state["state"], "deprecated")
        self.assertFalse(state["active"])

    def test_active_skill_deprecates_if_supporting_evidence_changes(self):
        verify_practice_run(self.root, self.goal_id, self.candidate_id,
                            self.run["run_id"])
        self.extra_run()
        self.assertTrue(self.reconcile()["active"])
        input_path = Path(self.run["artifacts"]["input"])
        value = json.loads(input_path.read_text())
        value["claim"] = "tampered"
        input_path.write_text(json.dumps(value))
        degraded = get_skill_lifecycle(self.root, self.goal_id, self.candidate_id)
        self.assertFalse(degraded["active"])
        self.assertEqual(degraded["state"], "deprecated")
        self.assertEqual(degraded["reason"], "supporting_evidence_invalidated")
        self.assertEqual(list_active_practice_skills(self.root, self.goal_id), [])

    def test_active_skill_deprecates_if_verifier_artifact_is_changed(self):
        first = verify_practice_run(self.root, self.goal_id, self.candidate_id,
                                    self.run["run_id"])
        self.extra_run()
        self.assertTrue(self.reconcile()["active"])
        path = Path(first["verifier_artifact"])
        value = json.loads(path.read_text())
        value["created_at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(value))
        state = get_skill_lifecycle(self.root, self.goal_id, self.candidate_id)
        self.assertEqual(state["state"], "deprecated")
        self.assertFalse(state["active"])

    def test_lifecycle_file_tamper_cannot_claim_active(self):
        state = self.reconcile()
        self.assertEqual(state["state"], "practicing")
        path = Path(state["artifact"])
        value = json.loads(path.read_text())
        value["state"] = "active"
        value["active"] = True
        path.write_text(json.dumps(value))
        safe = get_skill_lifecycle(self.root, self.goal_id, self.candidate_id)
        self.assertFalse(safe["active"])
        self.assertEqual(safe["state"], "deprecated")

    def test_three_success_policy_requires_three_distinct_runs(self):
        verify_practice_run(self.root, self.goal_id, self.candidate_id,
                            self.run["run_id"])
        self.extra_run()
        two = self.reconcile(required_successes=3)
        self.assertFalse(two["active"])
        with self.assertRaises(ValueError):
            self.reconcile(required_successes=2)
        self.extra_run()
        three = self.reconcile(required_successes=3)
        self.assertTrue(three["active"])
        self.assertEqual(three["success_count"], 3)
        self.assertEqual(three["evidence_strength"], "three_distinct_independent_runs")

    def test_explicit_entrypoint_does_not_run_executor(self):
        decision = verify_practice_run(self.root, self.goal_id, self.candidate_id,
                                       self.run["run_id"])
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "sira.learning_goal_skill_lifecycle",
             "--root", str(self.root), self.goal_id, self.candidate_id],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        state = json.loads(completed.stdout)
        self.assertEqual(state["state"], "independently_verified")
        self.assertEqual(state["evidence"][0]["run_id"], decision["run_id"])
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])

    def test_candidate_revocation_deprecates_active_skill(self):
        verify_practice_run(self.root, self.goal_id, self.candidate_id,
                            self.run["run_id"])
        self.extra_run()
        self.assertTrue(self.reconcile()["active"])
        self.store.set_status(self.goal_id, "paused")
        state = get_skill_lifecycle(self.root, self.goal_id, self.candidate_id)
        self.assertEqual(state["state"], "deprecated")
        self.assertFalse(state["active"])

    def test_unverified_knowledge_cannot_create_active_skill(self):
        never = "lsc_" + "e" * 24
        with self.assertRaises(ValueError):
            reconcile_skill_lifecycle(self.root, self.goal_id, never)
        self.assertEqual(list_active_practice_skills(self.root, self.goal_id), [])

    def test_policy_is_bounded_and_configurable(self):
        with self.assertRaises(ValueError):
            self.reconcile(required_successes=1)
        with self.assertRaises(ValueError):
            self.reconcile(required_successes=4)
        verify_practice_run(self.root, self.goal_id, self.candidate_id,
                            self.run["run_id"])
        self.extra_run()
        self.assertEqual(self.reconcile(required_successes=3)["state"],
                         "independently_verified")


if __name__ == "__main__":
    unittest.main()
