from pathlib import Path
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.code_writer import CodeModelBatch
from sira.engineering_authorization import (
    evaluate_engineering_authorization,
)
from sira.engineering_evaluator import (
    evaluate_engineering_candidate,
)
from sira.engineering_promotion import (
    EngineeringPromotionError,
    promote_engineering_candidate,
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

    def __init__(
        self,
        path: str = "src/app.py",
        content: str = "def value():\n    return 2\n",
    ):
        self.path = path
        self.content = content

    def generate_patch(self, payload, schema):
        return CodeModelBatch(
            {
                "summary": "fixture change",
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


def failing_runner(
    argv,
    *,
    cwd,
    timeout_seconds,
    max_output_bytes,
):
    payload = b"fixture failure"
    return {
        "status": "failed",
        "returncode": 1,
        "timed_out": False,
        "output_limit_exceeded": False,
        "output_bytes": len(payload),
        "output_sha256":
            hashlib.sha256(payload).hexdigest(),
        "duration_ms": 1,
        "diagnostic_text":
            "src/app.py:1:1: fixture failure",
    }


def exploding_runner(
    argv,
    *,
    cwd,
    timeout_seconds,
    max_output_bytes,
):
    raise RuntimeError("fixture verifier crash")


class EngineeringPromotionTests(unittest.TestCase):
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

    def make_chain(
        self,
        base: Path,
        *,
        model: FixtureModel | None = None,
        task_path: str = "src/app.py",
    ):
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
        selected = model or FixtureModel()
        attempt = (
            prepare_verified_engineering_candidate(
                root,
                {
                    "task_id": "task-promote",
                    "instruction":
                        "Apply the requested engineering change.",
                    "target_paths": [task_path],
                },
                base / "writer-workspace",
                EngineeringResearchBackedWriter(
                    root,
                    selected,
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
        authorization = (
            evaluate_engineering_authorization(
                root,
                attempt,
                evaluator,
            )
        )
        self.assertEqual(
            authorization["decision"],
            "allow",
        )
        return (
            root,
            attempt,
            evaluator,
            authorization,
        )

    def test_clean_authorized_candidate_promotes_transactionally(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (
                root,
                attempt,
                evaluator,
                authorization,
            ) = self.make_chain(base)

            report = promote_engineering_candidate(
                root,
                attempt,
                evaluator,
                authorization,
                base / "transaction",
                command_runner=passing_runner,
            )

            main_text = (
                root / "src/app.py"
            ).read_text(encoding="utf-8")
            audit = (
                base
                / "transaction"
                / "engineering_promotion_report.json"
            )
            audit_exists = audit.is_file()
            audit_payload = (
                json.loads(
                    audit.read_text(
                        encoding="utf-8"
                    )
                )
                if audit_exists
                else None
            )

        self.assertEqual(
            report["status"],
            "promoted",
        )
        self.assertEqual(
            report["decision_code"],
            "engineering_promotion_committed",
        )
        self.assertTrue(
            report["promotion_performed"]
        )
        self.assertFalse(
            report["rollback_performed"]
        )
        self.assertIn("return 2", main_text)
        self.assertTrue(
            report["post_verification_isolated"]
        )
        self.assertFalse(
            report["main_tree_command_execution"]
        )
        self.assertFalse(
            report["package_installation_performed"]
        )
        self.assertTrue(audit_exists)
        self.assertEqual(
            report["audit_path"],
            str(audit),
        )
        self.assertIsInstance(
            audit_payload,
            dict,
        )
        self.assertEqual(
            audit_payload["decision_code"],
            "engineering_promotion_committed",
        )

    def test_stale_main_is_denied_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (
                root,
                attempt,
                evaluator,
                authorization,
            ) = self.make_chain(base)

            self.write(
                root,
                "src/app.py",
                "def value():\n    return 9\n",
            )
            report = promote_engineering_candidate(
                root,
                attempt,
                evaluator,
                authorization,
                base / "transaction",
                command_runner=passing_runner,
            )

            main_text = (
                root / "src/app.py"
            ).read_text(encoding="utf-8")

        self.assertEqual(
            report["status"],
            "denied",
        )
        self.assertFalse(
            report["promotion_performed"]
        )
        self.assertIn("return 9", main_text)

    def test_tampered_authorization_fingerprint_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (
                root,
                attempt,
                evaluator,
                authorization,
            ) = self.make_chain(base)

            forged = copy.deepcopy(
                authorization
            )
            forged["authorization"][
                "fingerprint"
            ] = "0" * 64

            report = promote_engineering_candidate(
                root,
                attempt,
                evaluator,
                forged,
                base / "transaction",
                command_runner=passing_runner,
            )

        self.assertEqual(
            report["decision_code"],
            "authorization_mismatch",
        )
        self.assertFalse(
            report["promotion_performed"]
        )

    def test_candidate_tamper_after_authorization_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (
                root,
                attempt,
                evaluator,
                authorization,
            ) = self.make_chain(base)

            candidate = Path(
                attempt["candidate_root"]
            )
            self.write(
                candidate,
                "src/app.py",
                "def value():\n    return 77\n",
            )

            report = promote_engineering_candidate(
                root,
                attempt,
                evaluator,
                authorization,
                base / "transaction",
                command_runner=passing_runner,
            )

        self.assertEqual(
            report["status"],
            "denied",
        )
        self.assertFalse(
            report["promotion_performed"]
        )

    def test_failed_post_verification_restores_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (
                root,
                attempt,
                evaluator,
                authorization,
            ) = self.make_chain(base)

            report = promote_engineering_candidate(
                root,
                attempt,
                evaluator,
                authorization,
                base / "transaction",
                command_runner=failing_runner,
            )
            text = (
                root / "src/app.py"
            ).read_text(encoding="utf-8")

        self.assertEqual(
            report["status"],
            "rolled_back",
        )
        self.assertEqual(
            report["decision_code"],
            "engineering_post_verification_failed",
        )
        self.assertTrue(
            report["rollback_performed"]
        )
        self.assertTrue(
            report["rollback_verified"]
        )
        self.assertFalse(
            report["requires_manual_recovery"]
        )
        self.assertIn("return 1", text)

    def test_verifier_exception_restores_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (
                root,
                attempt,
                evaluator,
                authorization,
            ) = self.make_chain(base)

            report = promote_engineering_candidate(
                root,
                attempt,
                evaluator,
                authorization,
                base / "transaction",
                command_runner=exploding_runner,
            )
            text = (
                root / "src/app.py"
            ).read_text(encoding="utf-8")

        self.assertEqual(
            report["status"],
            "rolled_back",
        )
        self.assertEqual(
            report["decision_code"],
            "engineering_post_verification_error",
        )
        self.assertTrue(
            report["rollback_verified"]
        )
        self.assertIn("return 1", text)

    def test_atomic_write_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (
                root,
                attempt,
                evaluator,
                authorization,
            ) = self.make_chain(base)

            with patch(
                "sira.engineering_promotion._atomic_replace",
                side_effect=OSError(
                    "fixture write failure"
                ),
            ):
                report = promote_engineering_candidate(
                    root,
                    attempt,
                    evaluator,
                    authorization,
                    base / "transaction",
                    command_runner=passing_runner,
                )

            text = (
                root / "src/app.py"
            ).read_text(encoding="utf-8")

        self.assertEqual(
            report["status"],
            "rolled_back",
        )
        self.assertEqual(
            report["decision_code"],
            "engineering_promotion_write_failed",
        )
        self.assertTrue(
            report["rollback_verified"]
        )
        self.assertIn("return 1", text)

    def test_failed_verification_removes_new_file_on_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            model = FixtureModel(
                path="src/new_feature.py",
                content="VALUE = 2\n",
            )
            (
                root,
                attempt,
                evaluator,
                authorization,
            ) = self.make_chain(
                base,
                model=model,
                task_path="src/new_feature.py",
            )

            report = promote_engineering_candidate(
                root,
                attempt,
                evaluator,
                authorization,
                base / "transaction",
                command_runner=failing_runner,
            )

        self.assertEqual(
            report["status"],
            "rolled_back",
        )
        self.assertTrue(
            report["rollback_verified"]
        )
        self.assertFalse(
            (
                root
                / "src/new_feature.py"
            ).exists()
        )

    def test_transaction_root_inside_main_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (
                root,
                attempt,
                evaluator,
                authorization,
            ) = self.make_chain(base)

            with self.assertRaises(
                EngineeringPromotionError
            ):
                promote_engineering_candidate(
                    root,
                    attempt,
                    evaluator,
                    authorization,
                    root / "runtime" / "bad-transaction",
                    command_runner=passing_runner,
                )

    def test_new_promotion_module_is_protected_from_sira_editor(self):
        self.assertIn(
            "src/sira/engineering_promotion.py",
            PROTECTED_PATHS,
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            main = base / "main"
            candidate = base / "candidate"
            main.mkdir()
            self.write(main, "sira.py", "")
            self.write(
                main,
                "src/sira/engineering_promotion.py",
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
                        "src/sira/engineering_promotion.py":
                            "PROTECTED = False\n"
                    },
                )


if __name__ == "__main__":
    unittest.main()
