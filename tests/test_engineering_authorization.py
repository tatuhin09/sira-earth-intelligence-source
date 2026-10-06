from pathlib import Path
import copy
import hashlib
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.code_writer import CodeModelBatch
from sira.engineering_authorization import (
    evaluate_engineering_authorization,
)
from sira.engineering_evaluator import (
    evaluate_engineering_candidate,
)
from sira.engineering_writer import (
    EngineeringResearchBackedWriter,
    prepare_verified_engineering_candidate,
)
from sira.self_modification import (
    CandidateEditError,
    PROTECTED_PATHS,
    ValidatedCandidateEditor,
    prepare_candidate_workspace,
)


class FixtureModel:
    name = "fixture_engineering_model"
    model_id = "fixture-v1"

    def generate_patch(self, payload, schema):
        return CodeModelBatch(
            {
                "summary": "fixture change",
                "edits": [{
                    "path": "src/app.py",
                    "content":
                        "def value():\n    return 2\n",
                    "reason": "fixture",
                }],
                "needs_more_context": False,
                "context_requests": [],
            },
            api_requests=0,
            input_tokens=0,
            output_tokens=0,
        )


def passing_runner(
    argv,
    *,
    cwd,
    timeout_seconds,
    max_output_bytes,
):
    return {
        "status": "passed",
        "returncode": 0,
        "timed_out": False,
        "output_limit_exceeded": False,
        "output_bytes": 0,
        "output_sha256":
            hashlib.sha256(b"").hexdigest(),
        "duration_ms": 1,
        "diagnostic_text": "",
    }


class EngineeringAuthorizationTests(unittest.TestCase):
    def write(
        self,
        root: Path,
        relative: str,
        content: str,
    ):
        path = root / relative
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        path.write_text(
            content,
            encoding="utf-8",
        )

    def make_chain(self, base: Path):
        root = base / "project"
        root.mkdir()
        self.write(
            root,
            "src/app.py",
            "def value():\n    return 1\n",
        )
        self.write(
            root,
            "tests/test_app.py",
            "from src.app import value\n",
        )
        task = {
            "task_id": "task-auth",
            "instruction":
                "Change value() so it returns 2.",
            "target_paths": ["src/app.py"],
        }
        attempt = (
            prepare_verified_engineering_candidate(
                root,
                task,
                base / "workspace",
                EngineeringResearchBackedWriter(
                    root,
                    FixtureModel(),
                ),
                command_runner=passing_runner,
            )
        )
        evaluator = (
            evaluate_engineering_candidate(
                root,
                attempt,
            )
        )
        return root, attempt, evaluator

    def test_clean_candidate_gets_checksum_only_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, attempt, evaluator = (
                self.make_chain(Path(tmp))
            )
            report = (
                evaluate_engineering_authorization(
                    root,
                    attempt,
                    evaluator,
                )
            )

        self.assertEqual(
            report["decision"],
            "allow",
        )
        self.assertEqual(
            report["decision_code"],
            "engineering_promotion_authorized",
        )
        self.assertTrue(
            report["promotion_allowed"]
        )
        self.assertFalse(
            report["promotion_performed"]
        )
        self.assertEqual(
            report["authority_scope"],
            "engineering_candidate_checksum_only",
        )
        self.assertTrue(
            report["authorization"][
                "checksum_only"
            ]
        )
        self.assertFalse(
            report["authorization"][
                "write_authority"
            ]
        )
        self.assertFalse(
            report["authorization"][
                "promotion_execution_authority"
            ]
        )
        self.assertEqual(
            len(
                report["authorization"][
                    "fingerprint"
                ]
            ),
            64,
        )
        self.assertEqual(
            report["binding"][
                "changed_files"
            ],
            ["src/app.py"],
        )
        self.assertEqual(
            report["failures"],
            [],
        )

    def test_authorization_module_is_registered_as_protected(self):
        self.assertIn(
            "src/sira/engineering_authorization.py",
            PROTECTED_PATHS,
        )

    def test_existing_sira_candidate_editor_cannot_edit_new_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            main = base / "main"
            candidate = base / "candidate"
            main.mkdir()
            self.write(main, "sira.py", "")
            self.write(
                main,
                "src/sira/engineering_authorization.py",
                "PROTECTED = True\n",
            )
            prepare_candidate_workspace(
                main,
                candidate,
            )
            with self.assertRaises(
                CandidateEditError
            ):
                ValidatedCandidateEditor().apply_text_edits(
                    candidate,
                    {
                        "src/sira/engineering_authorization.py":
                            "PROTECTED = False\n"
                    },
                )

    def test_stale_main_denies_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, attempt, evaluator = (
                self.make_chain(base)
            )
            self.write(
                root,
                "src/app.py",
                "def value():\n    return 99\n",
            )
            report = (
                evaluate_engineering_authorization(
                    root,
                    attempt,
                    evaluator,
                )
            )

        self.assertEqual(
            report["decision"],
            "deny",
        )
        self.assertEqual(
            report["decision_code"],
            "stale_candidate_base",
        )
        self.assertFalse(
            report["promotion_allowed"]
        )

    def test_candidate_tamper_denies_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, attempt, evaluator = (
                self.make_chain(base)
            )
            candidate = Path(
                attempt["candidate_root"]
            )
            self.write(
                candidate,
                "src/extra.py",
                "VALUE = 3\n",
            )
            report = (
                evaluate_engineering_authorization(
                    root,
                    attempt,
                    evaluator,
                )
            )

        self.assertEqual(
            report["decision_code"],
            "engineering_candidate_integrity_failed",
        )
        self.assertIn(
            "changed_set_mismatch",
            report["failures"],
        )

    def test_candidate_hash_tamper_denies_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, attempt, evaluator = (
                self.make_chain(base)
            )
            candidate = Path(
                attempt["candidate_root"]
            )
            self.write(
                candidate,
                "src/app.py",
                "def value():\n    return 8\n",
            )
            report = (
                evaluate_engineering_authorization(
                    root,
                    attempt,
                    evaluator,
                )
            )

        self.assertEqual(
            report["decision_code"],
            "engineering_candidate_integrity_failed",
        )
        self.assertIn(
            "candidate_hash_mismatch",
            report["failures"],
        )

    def test_noneligible_or_tampered_evaluator_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, attempt, evaluator = (
                self.make_chain(Path(tmp))
            )
            forged = copy.deepcopy(
                evaluator
            )
            forged[
                "promotion_candidate_eligible"
            ] = False
            report = (
                evaluate_engineering_authorization(
                    root,
                    attempt,
                    forged,
                )
            )

        self.assertEqual(
            report["decision_code"],
            "engineering_evaluator_not_eligible",
        )
        self.assertFalse(
            report["promotion_allowed"]
        )

    def test_dirty_verification_is_denied_even_with_old_accepting_evaluator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, attempt, evaluator = (
                self.make_chain(Path(tmp))
            )
            dirty = copy.deepcopy(
                attempt
            )
            dirty["verification"][
                "diagnostic_count"
            ] = 1
            report = (
                evaluate_engineering_authorization(
                    root,
                    dirty,
                    evaluator,
                )
            )

        self.assertEqual(
            report["decision_code"],
            "engineering_verification_not_clean",
        )
        self.assertIn(
            "verification_diagnostics_not_clean",
            report["failures"],
        )

    def test_evaluator_binding_mismatch_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, attempt, evaluator = (
                self.make_chain(Path(tmp))
            )
            forged = copy.deepcopy(
                evaluator
            )
            forged["diff"][
                "actual_changed_files"
            ] = ["src/not-app.py"]
            report = (
                evaluate_engineering_authorization(
                    root,
                    attempt,
                    forged,
                )
            )

        self.assertEqual(
            report["decision_code"],
            "engineering_candidate_integrity_failed",
        )
        self.assertIn(
            "engineering_evaluator_binding_mismatch",
            report["failures"],
        )


if __name__ == "__main__":
    unittest.main()
