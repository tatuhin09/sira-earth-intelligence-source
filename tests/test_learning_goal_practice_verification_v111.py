"""A75: practice execution is not independent verification or skill mastery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_consolidation import LearningConsolidationStore
from sira.learning_goal_practice import execute_practice_candidate
from sira.learning_goal_practice_verification import verify_practice_run
from sira.learning_goal_security_claim import record_security_risk_claim
from sira.learning_goal_skill_candidates import advisory_skill_candidates
from sira.learning_goals import LearningGoalStore


class IndependentPracticeVerificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create("Cybersecurity fundamentals and defense",
                                      public_research_allowed=True)
        self.focus = "Cybersecurity risk assessment and threat modeling"
        self.store.set_plan(self.goal["goal_id"], [self.focus, "Network security controls"])
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
            "research_query": self.focus, "status": "quotes_need_claim_verification",
            "documents": [
                {"url": url, "host": host, "title": title, "text": text,
                 "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                 "retrieved_at": "2026-09-27T10:00:00+00:00", "verified": False}
                for url, host, title, text in documents
            ], "metadata_evidence": [], "source_failures": [],
            "verified_claims_recorded": False,
        }), encoding="utf-8")
        record_security_risk_claim(
            self.root, self.goal["goal_id"], self.artifact,
            expected_artifact_sha256=hashlib.sha256(self.artifact.read_bytes()).hexdigest())
        self.candidate = advisory_skill_candidates(
            self.root, self.store.get(self.goal["goal_id"]))[0]
        self.run = execute_practice_candidate(
            self.root, self.goal["goal_id"], self.candidate["candidate_id"])
        self.assertEqual(self.run["status"], "completed")

    def verify(self, candidate_id=None, run_id=None):
        return verify_practice_run(
            self.root, self.goal["goal_id"],
            candidate_id or self.candidate["candidate_id"],
            run_id or self.run["run_id"])

    def rewrite(self, key, update):
        path = Path(self.run["artifacts"][key])
        data = json.loads(path.read_text(encoding="utf-8"))
        update(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_executor_cannot_self_certify(self):
        record = json.loads(Path(self.run["artifacts"]["record"]).read_text())
        self.assertNotIn("independently_verified", record)
        self.assertNotIn("practice_verified", record)
        self.assertFalse((self.root / "memory/learning_practice_verifications").exists())
        self.rewrite("record", lambda row: row.update({"practice_verified": True}))
        decision = self.verify()
        self.assertEqual(decision["status"], "rejected")
        self.assertEqual(decision["failure_reason"], "contaminated_practice_record")
        self.assertFalse(decision["skill_activated"])

    def test_valid_separate_verifier_passes_with_reproducible_evidence(self):
        original = Path(self.run["artifacts"]["record"]).read_bytes()
        decision = self.verify()
        self.assertEqual(decision["status"], "verified")
        self.assertTrue(decision["practice_verified"])
        self.assertEqual(decision["expected_result"]["source_hosts"],
                         ["cheatsheetseries.owasp.org", "www.cisa.gov"])
        self.assertEqual(decision["observed_result"], decision["expected_result"])
        self.assertEqual(decision["verifier_evidence"]["independent_host_count"], 2)
        self.assertEqual(decision["reproducibility"]["algorithm"],
                         "independent_provenance_v1")
        self.assertEqual(decision["reproducibility"]["record_sha256"],
                         hashlib.sha256(original).hexdigest())
        self.assertEqual(Path(self.run["artifacts"]["record"]).read_bytes(), original)
        saved = Path(decision["verifier_artifact"])
        self.assertEqual(json.loads(saved.read_text()), decision)
        self.assertEqual(saved.stat().st_mode & 0o077, 0)
        repeated = self.verify()
        self.assertEqual(repeated["reproducibility"]["decision_fingerprint"],
                         decision["reproducibility"]["decision_fingerprint"])
        self.assertEqual(repeated["status"], "verified")
        self.assertFalse(decision["skill_activated"])
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])

    def test_wrong_worker_output_is_rejected(self):
        self.rewrite("result", lambda row: row.update({"source_hosts": ["wrong.example"]}))
        decision = self.verify()
        self.assertEqual(decision["status"], "rejected")
        self.assertEqual(decision["failure_reason"], "practice_result_mismatch")
        self.assertEqual(decision["observed_result"]["source_hosts"], ["wrong.example"])
        self.assertFalse(decision["practice_verified"])

    def test_forged_prior_verifier_record_cannot_self_certify(self):
        initial = self.verify()
        path = Path(initial["verifier_artifact"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["skill_activated"] = True
        path.write_text(json.dumps(saved), encoding="utf-8")
        repeated = self.verify()
        self.assertFalse(repeated["skill_activated"])
        self.assertEqual(json.loads(path.read_text())["skill_activated"], False)

    def test_nonfinite_worker_output_is_rejected_with_a_record(self):
        path = Path(self.run["artifacts"]["result"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["source_hosts"].append(float("nan"))
        path.write_text(json.dumps(data), encoding="utf-8")
        decision = self.verify()
        self.assertEqual(decision["status"], "rejected")
        self.assertEqual(decision["failure_reason"], "verification_incomplete")
        self.assertFalse(decision["practice_verified"])

    def test_nonregular_result_cannot_block_verification(self):
        result_path = Path(self.run["artifacts"]["result"])
        result_path.unlink()
        os.mkfifo(result_path)
        code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "from pathlib import Path; "
                "from sira.learning_goal_practice_verification import verify_practice_run; "
                "r=verify_practice_run(Path(sys.argv[2]), *sys.argv[3:]); "
                "print(r['failure_reason'])")
        completed = subprocess.run(
            [sys.executable, "-c", code, str(Path(__file__).resolve().parents[1] / "src"),
             str(self.root), self.goal["goal_id"], self.candidate["candidate_id"],
             self.run["run_id"]], capture_output=True, text=True, timeout=2)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "practice_artifact_unsafe")

    def test_missing_or_incomplete_evidence_is_rejected(self):
        Path(self.run["artifacts"]["result"]).unlink()
        decision = self.verify()
        self.assertEqual(decision["status"], "rejected")
        self.assertEqual(decision["failure_reason"], "practice_artifact_missing")
        self.assertIsNone(decision["observed_result"])

    def test_incomplete_run_provenance_is_not_evidence(self):
        self.rewrite("record", lambda row: row.update({"attempted_at_epoch": None}))
        decision = self.verify()
        self.assertEqual(decision["status"], "rejected")
        self.assertEqual(decision["failure_reason"], "verification_incomplete")

    def test_tampered_input_and_artifact_identifier_are_rejected(self):
        self.rewrite("input", lambda row: row.update({"claim": "unrelated"}))
        decision = self.verify()
        self.assertEqual(decision["status"], "rejected")
        self.assertEqual(decision["failure_reason"], "practice_input_mismatch")
        self.rewrite("record", lambda row: row["artifacts"].update(
            {"result": str(self.root / "memory/somewhere_else.json")}))
        decision = self.verify()
        self.assertEqual(decision["failure_reason"], "practice_artifact_mismatch")

    def test_failed_practice_never_creates_active_skill(self):
        rejected = execute_practice_candidate(
            self.root, self.goal["goal_id"], "lsc_" + "f" * 24)
        self.assertEqual(rejected["status"], "rejected")
        decision = self.verify(candidate_id=rejected["candidate_id"],
                               run_id=rejected["run_id"])
        self.assertEqual(decision["status"], "rejected")
        self.assertEqual(decision["failure_reason"], "practice_not_completed")
        self.assertFalse(decision["skill_activated"])
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])

    def test_fresh_verified_source_is_required_after_a74_execution(self):
        source = json.loads(self.artifact.read_text())
        source["documents"][0]["text"] += " changed after practice"
        self.artifact.write_text(json.dumps(source), encoding="utf-8")
        decision = self.verify()
        self.assertEqual(decision["status"], "rejected")
        self.assertEqual(decision["failure_reason"], "candidate_not_verified")

    def test_separate_command_is_explicit_and_a74_run_stays_compatible(self):
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "sira.learning_goal_practice_verification",
             "--root", str(self.root), self.goal["goal_id"],
             self.candidate["candidate_id"], self.run["run_id"]],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "verified")
        self.assertEqual(json.loads(Path(self.run["artifacts"]["record"]).read_text()),
                         self.run)
        self.assertEqual(LearningConsolidationStore(self.root).list_skills(), [])


if __name__ == "__main__":
    unittest.main()
