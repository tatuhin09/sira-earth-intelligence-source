from pathlib import Path
import hashlib
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.code_writer import CodeModelBatch
from sira.engineering_editing import EngineeringCandidateEditError
from sira.engineering_writer import (
    EngineeringResearchBackedWriter,
    EngineeringWriterOutputError,
    build_engineering_writer_context,
    prepare_verified_engineering_candidate,
)


class FixtureModel:
    name = "fixture_engineering_model"
    model_id = "fixture-v1"

    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def generate_patch(self, payload, schema):
        self.payloads.append(payload)
        if not self.responses:
            raise AssertionError("unexpected model call")
        return CodeModelBatch(
            self.responses.pop(0),
            api_requests=0,
            input_tokens=0,
            output_tokens=0,
        )


def runner(status="passed", text=""):
    def run(
        argv,
        *,
        cwd,
        timeout_seconds,
        max_output_bytes,
    ):
        payload = text.encode()
        return {
            "status": status,
            "returncode": 0 if status == "passed" else 1,
            "timed_out": status == "timeout",
            "output_limit_exceeded":
                status == "output_limit",
            "output_bytes": len(payload),
            "output_sha256":
                hashlib.sha256(payload).hexdigest(),
            "duration_ms": 1,
            "diagnostic_text": text,
        }
    return run


class EngineeringWriterTests(unittest.TestCase):
    def write(
        self,
        root: Path,
        relative: str,
        content: str,
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def make_python_project(self, root: Path):
        self.write(
            root,
            "src/app.py",
            "def value():\n    return 1\n",
        )
        self.write(
            root,
            "src/helper.py",
            "def helper():\n    return 10\n",
        )
        self.write(
            root,
            "tests/test_app.py",
            "from src.app import value\n",
        )

    def task(self):
        return {
            "task_id": "task-1",
            "instruction":
                "Change value() so it returns 2.",
            "target_paths": ["src/app.py"],
        }

    def test_context_is_bounded_and_excludes_sensitive_vendor_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            self.make_python_project(root)
            self.write(root, ".env", "TOKEN=hidden\n")
            self.write(
                root,
                "vendor/pkg/private.py",
                "SECRET='x'\n",
            )

            context = build_engineering_writer_context(
                root,
                self.task(),
            )

        self.assertEqual(
            context["schema"],
            "sira.engineering_writer_context.v1",
        )
        self.assertTrue(
            any(
                row["path"] == "src/app.py"
                for row in context["files"]
            )
        )
        rendered = repr(context)
        self.assertNotIn("TOKEN=hidden", rendered)
        self.assertNotIn("vendor/pkg/private.py", rendered)
        self.assertFalse(
            context["editable_path_policy"][
                "dependency_manifest_editing_allowed"
            ]
        )

    def test_existing_target_requires_full_context_and_gets_one_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            self.make_python_project(root)
            task = {
                "task_id": "task-2",
                "instruction":
                    "Use helper() while changing value().",
                "target_paths": ["src/app.py"],
            }
            model = FixtureModel([
                {
                    "summary": "need helper",
                    "edits": [],
                    "needs_more_context": True,
                    "context_requests": ["src/helper.py"],
                },
                {
                    "summary": "use helper",
                    "edits": [{
                        "path": "src/app.py",
                        "content":
                            "from .helper import helper\n\n"
                            "def value():\n"
                            "    return helper()\n",
                        "reason": "reuse helper",
                    }],
                    "needs_more_context": False,
                    "context_requests": [],
                },
            ])
            writer = EngineeringResearchBackedWriter(
                root,
                model,
            )
            edits = writer.propose_text_edits(task)

        self.assertIn("src/app.py", edits)
        self.assertEqual(len(model.payloads), 2)
        self.assertEqual(
            model.payloads[1]["context_tier"],
            "expanded",
        )
        self.assertIn(
            "src/helper.py",
            [
                row["path"]
                for row in model.payloads[1]["files"]
            ],
        )

    def test_verified_candidate_changes_only_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            root.mkdir()
            self.make_python_project(root)
            model = FixtureModel([{
                "summary": "change value",
                "edits": [{
                    "path": "src/app.py",
                    "content":
                        "def value():\n    return 2\n",
                    "reason": "requested",
                }],
                "needs_more_context": False,
                "context_requests": [],
            }])
            result = prepare_verified_engineering_candidate(
                root,
                self.task(),
                base / "workspace",
                EngineeringResearchBackedWriter(
                    root,
                    model,
                ),
                command_runner=runner("passed"),
            )

            source = (
                root / "src/app.py"
            ).read_text(encoding="utf-8")
            candidate = (
                Path(result["candidate_root"])
                / "src/app.py"
            ).read_text(encoding="utf-8")

        self.assertEqual(
            result["status"],
            "verified_candidate_ready",
        )
        self.assertIn("return 1", source)
        self.assertIn("return 2", candidate)
        self.assertTrue(
            result["verification_executed"]
        )
        self.assertTrue(
            result["verification"]["overall_passed"]
        )
        self.assertFalse(
            result["promotion_authorized"]
        )

    def test_failed_verification_returns_structured_advisory_without_raw_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            root.mkdir()
            self.make_python_project(root)
            model = FixtureModel([{
                "summary": "bad fixture",
                "edits": [{
                    "path": "src/app.py",
                    "content":
                        "def value():\n    return missing\n",
                    "reason": "fixture",
                }],
                "needs_more_context": False,
                "context_requests": [],
            }])
            raw = (
                "src/app.py:2:5: "
                "NameError: token=super-secret"
            )
            result = prepare_verified_engineering_candidate(
                root,
                self.task(),
                base / "workspace",
                EngineeringResearchBackedWriter(
                    root,
                    model,
                ),
                command_runner=runner(
                    "failed",
                    raw,
                ),
            )

        self.assertEqual(
            result["status"],
            "candidate_rejected",
        )
        self.assertGreaterEqual(
            result["diagnostic_advisory"][
                "diagnostic_count"
            ],
            1,
        )
        self.assertEqual(
            result["diagnostic_advisory"][
                "repair_authority"
            ],
            "advisory_only",
        )
        rendered = repr(result)
        self.assertNotIn("super-secret", rendered)
        self.assertNotIn("diagnostic_text", rendered)

    def test_no_edit_is_non_mutating_and_does_not_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            root.mkdir()
            self.make_python_project(root)
            model = FixtureModel([{
                "summary": "already correct",
                "edits": [],
                "needs_more_context": False,
                "context_requests": [],
            }])
            result = prepare_verified_engineering_candidate(
                root,
                self.task(),
                base / "workspace",
                EngineeringResearchBackedWriter(
                    root,
                    model,
                ),
                command_runner=runner("passed"),
            )

        self.assertEqual(
            result["status"],
            "no_edit_generated",
        )
        self.assertIsNone(result["candidate_root"])
        self.assertFalse(
            result["verification_executed"]
        )
        self.assertFalse(
            result["main_tree_modified"]
        )

    def test_manifest_edit_is_rejected_by_candidate_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            root.mkdir()
            self.make_python_project(root)
            self.write(
                root,
                "pyproject.toml",
                "[project]\nname='x'\n",
            )
            model = FixtureModel([{
                "summary": "unsafe",
                "edits": [{
                    "path": "pyproject.toml",
                    "content":
                        "[project]\nname='changed'\n",
                    "reason": "unsafe fixture",
                }],
                "needs_more_context": False,
                "context_requests": [],
            }])
            with self.assertRaises(
                EngineeringCandidateEditError
            ):
                prepare_verified_engineering_candidate(
                    root,
                    {
                        "instruction":
                            "Change dependency metadata.",
                        "target_paths": [],
                    },
                    base / "workspace",
                    EngineeringResearchBackedWriter(
                        root,
                        model,
                    ),
                    command_runner=runner("passed"),
                )

    def test_second_context_request_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            self.make_python_project(root)
            model = FixtureModel([
                {
                    "summary": "need helper",
                    "edits": [],
                    "needs_more_context": True,
                    "context_requests": ["src/helper.py"],
                },
                {
                    "summary": "need more",
                    "edits": [],
                    "needs_more_context": True,
                    "context_requests": ["src/app.py"],
                },
            ])
            writer = EngineeringResearchBackedWriter(
                root,
                model,
            )
            with self.assertRaises(
                EngineeringWriterOutputError
            ):
                writer.propose_text_edits(
                    self.task()
                )


if __name__ == "__main__":
    unittest.main()
