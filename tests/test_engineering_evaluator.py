from pathlib import Path
import copy
import hashlib
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.code_writer import CodeModelBatch
from sira.engineering_evaluator import evaluate_engineering_candidate
from sira.engineering_writer import (
    EngineeringResearchBackedWriter,
    prepare_verified_engineering_candidate,
)


class FixtureModel:
    name = "fixture_engineering_model"
    model_id = "fixture-v1"

    def __init__(self, *, path="src/app.py", content=None):
        self.path = path
        self.content = content or "def value():\n    return 2\n"

    def generate_patch(self, payload, schema):
        return CodeModelBatch(
            {
                "summary": "fixture",
                "edits": [{
                    "path": self.path,
                    "content": self.content,
                    "reason": "fixture",
                }],
                "needs_more_context": False,
                "context_requests": [],
            },
            api_requests=0,
            input_tokens=0,
            output_tokens=0,
        )


def passing_runner(argv, *, cwd, timeout_seconds, max_output_bytes):
    return {
        "status": "passed",
        "returncode": 0,
        "timed_out": False,
        "output_limit_exceeded": False,
        "output_bytes": 0,
        "output_sha256": hashlib.sha256(b"").hexdigest(),
        "duration_ms": 1,
        "diagnostic_text": "",
    }


class EngineeringEvaluatorTests(unittest.TestCase):
    def write(self, root: Path, relative: str, content: str):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def make_attempt(self, base: Path):
        root = base / "project"
        root.mkdir()
        self.write(root, "src/app.py", "def value():\n    return 1\n")
        self.write(
            root,
            "tests/test_app.py",
            "from src.app import value\n",
        )
        task = {
            "task_id": "task-1",
            "instruction": "Change value() to return 2.",
            "target_paths": ["src/app.py"],
        }
        attempt = prepare_verified_engineering_candidate(
            root,
            task,
            base / "workspace",
            EngineeringResearchBackedWriter(root, FixtureModel()),
            command_runner=passing_runner,
        )
        return root, attempt

    def test_clean_verified_candidate_is_only_eligible_not_authorized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, attempt = self.make_attempt(Path(tmp))
            report = evaluate_engineering_candidate(root, attempt)

        self.assertEqual(report["decision"], "accept")
        self.assertEqual(
            report["decision_code"],
            "engineering_candidate_verified",
        )
        self.assertEqual(
            report["recommendation"],
            "eligible_for_protected_gate",
        )
        self.assertTrue(report["promotion_candidate_eligible"])
        self.assertFalse(report["promotion_authorized"])
        self.assertFalse(report["promotion_performed"])
        self.assertFalse(report["authority_granted"])
        self.assertFalse(report["main_tree_modified"])
        self.assertTrue(report["checks"]["candidate_base_current"])
        self.assertTrue(report["checks"]["verification_clean"])
        self.assertTrue(report["checks"]["diagnostic_advisory_clean"])

    def test_main_change_after_candidate_creation_blocks_as_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, attempt = self.make_attempt(base)
            self.write(root, "src/app.py", "def value():\n    return 99\n")
            report = evaluate_engineering_candidate(root, attempt)

        self.assertEqual(report["decision"], "block")
        self.assertEqual(report["decision_code"], "stale_candidate_base")
        self.assertFalse(report["promotion_candidate_eligible"])

    def test_undeclared_candidate_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, attempt = self.make_attempt(base)
            candidate = Path(attempt["candidate_root"])
            self.write(candidate, "src/extra.py", "VALUE = 3\n")
            report = evaluate_engineering_candidate(root, attempt)

        self.assertEqual(report["decision"], "reject")
        self.assertEqual(report["decision_code"], "candidate_integrity_failed")
        self.assertIn("changed_set_mismatch", report["risk_flags"])

    def test_candidate_hash_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, attempt = self.make_attempt(base)
            candidate = Path(attempt["candidate_root"])
            self.write(candidate, "src/app.py", "def value():\n    return 8\n")
            report = evaluate_engineering_candidate(root, attempt)

        self.assertEqual(report["decision_code"], "candidate_integrity_failed")
        self.assertIn("candidate_hash_mismatch", report["risk_flags"])

    def test_symlink_sensitive_and_unexpected_artifacts_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, attempt = self.make_attempt(base)
            candidate = Path(attempt["candidate_root"])
            outside = base / "outside.py"
            outside.write_text("VALUE=1\n", encoding="utf-8")
            (candidate / "src" / "linked.py").symlink_to(outside)
            self.write(candidate, ".env", "TOKEN=hidden\n")
            self.write(candidate, "src/blob.bin", "not allowed\n")
            report = evaluate_engineering_candidate(root, attempt)

        self.assertEqual(report["decision_code"], "candidate_integrity_failed")
        self.assertIn("symlink_present", report["risk_flags"])
        self.assertIn("sensitive_artifact_present", report["risk_flags"])
        self.assertIn("unexpected_candidate_file", report["risk_flags"])

    def test_removed_source_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, attempt = self.make_attempt(base)
            candidate = Path(attempt["candidate_root"])
            (candidate / "tests" / "test_app.py").unlink()
            report = evaluate_engineering_candidate(root, attempt)

        self.assertEqual(report["decision_code"], "candidate_integrity_failed")
        self.assertIn("file_removed", report["risk_flags"])

    def test_non_clean_verification_or_diagnostics_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, attempt = self.make_attempt(Path(tmp))
            dirty = copy.deepcopy(attempt)
            dirty["verification"]["diagnostic_count"] = 1
            dirty["verification"]["overall_passed"] = True
            dirty["diagnostic_advisory"]["diagnostic_count"] = 1
            report = evaluate_engineering_candidate(root, dirty)

        self.assertEqual(report["decision"], "reject")
        self.assertEqual(report["decision_code"], "verification_not_clean")
        self.assertIn(
            "verification_diagnostics_not_clean",
            report["risk_flags"],
        )
        self.assertIn(
            "diagnostic_advisory_not_clean",
            report["risk_flags"],
        )

    def test_forged_authority_or_manifest_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, attempt = self.make_attempt(Path(tmp))
            forged = copy.deepcopy(attempt)
            forged["writer_report"]["promotion_authorized"] = True
            report = evaluate_engineering_candidate(root, forged)
            self.assertEqual(
                report["decision_code"],
                "attempt_integrity_failed",
            )

        with tempfile.TemporaryDirectory() as tmp:
            root, attempt = self.make_attempt(Path(tmp))
            forged = copy.deepcopy(attempt)
            forged["edit_candidate"]["candidate_manifest"][
                "candidate_root"
            ] = "/tmp/not-the-candidate"
            report = evaluate_engineering_candidate(root, forged)
            self.assertEqual(
                report["decision_code"],
                "attempt_integrity_failed",
            )


if __name__ == "__main__":
    unittest.main()
