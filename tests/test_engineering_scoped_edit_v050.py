from __future__ import annotations

import tempfile
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.code_writer import CodeModelBatch
from sira.engineering_scoped_edit import (
    EngineeringScopedEditError,
    ScopedPythonSymbolEdit,
    materialize_engineering_edits,
)
from sira.engineering_writer import (
    ENGINEERING_PATCH_SCHEMA,
    EngineeringResearchBackedWriter,
    build_engineering_writer_context,
)


SOURCE = """class Demo:
    def target(self, value: int) -> int:
        if value > 10:
            return value
        if value > 0:
            return value + 1
        return 0

    def untouched(self) -> str:
        return "ok"
"""


def task():
    return {
        "task_id": "v050-test",
        "instruction": (
            "Improve src/demo.py:target with the smallest behavior-preserving "
            "change. Primary symbol: target."
        ),
        "target_paths": ["src/demo.py"],
        "target": {"path": "src/demo.py", "symbol": "target"},
        "language_hint": "python",
        "success_criteria": {
            "structural_goal": {
                "metric": "branch_points",
                "baseline": 2,
                "target_max": 1,
            }
        },
        "diagnostics": [],
    }


class FixtureModel:
    name = "fixture_scoped_model"
    model_id = "fixture"

    def __init__(self, data):
        self.data = data
        self.payloads = []

    def generate_patch(self, payload, schema):
        self.payloads.append(payload)
        return CodeModelBatch(self.data)


class EngineeringScopedEditV050Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "demo.py").write_text(SOURCE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_context_exposes_exact_python_symbol_scope(self):
        context = build_engineering_writer_context(self.root, task())
        scope = context["scoped_edit"]
        self.assertEqual(scope["mode"], "python_symbol_block")
        self.assertEqual(scope["path"], "src/demo.py")
        self.assertEqual(scope["symbol"], "target")
        self.assertGreater(scope["end_line"], scope["start_line"])
        self.assertEqual(len(scope["source_sha256"]), 64)
        self.assertEqual(len(scope["symbol_sha256"]), 64)

    def test_schema_supports_optional_symbol_field(self):
        props = ENGINEERING_PATCH_SCHEMA["properties"]["edits"]["items"]["properties"]
        self.assertIn("symbol", props)

    def test_scoped_target_reconstructs_full_file_without_touching_main(self):
        replacement = """def target(self, value: int) -> int:
    if value > 10:
        return value
    return value + 1 if value > 0 else 0
"""
        model = FixtureModel({
            "summary": "Extract one private sibling helper.",
            "edits": [{
                "path": "src/demo.py",
                "symbol": "target",
                "content": replacement,
                "reason": "Reduce branch points in the selected symbol.",
            }],
        })
        writer = EngineeringResearchBackedWriter(self.root, model)
        edits = writer.propose_text_edits(task())

        self.assertEqual(
            (self.root / "src" / "demo.py").read_text(encoding="utf-8"),
            SOURCE,
        )
        materialized = edits["src/demo.py"]
        self.assertIn("def target(self, value: int) -> int:", materialized)
        self.assertIn("    def untouched(self) -> str:", materialized)
        metadata = writer.last_report["edit_metadata"][0]
        self.assertEqual(metadata["scope"], "python_symbol_block")
        self.assertEqual(metadata["symbol"], "target")

    def test_signature_change_is_rejected(self):
        context = build_engineering_writer_context(self.root, task())
        with self.assertRaises(EngineeringScopedEditError):
            materialize_engineering_edits(
                self.root,
                {"src/demo.py": ScopedPythonSymbolEdit(
                    "target",
                    "def target(self, value, extra):\n    return value\n",
                )},
                context,
                max_edit_file_bytes=256 * 1024,
                max_patch_bytes=512 * 1024,
            )

    def test_non_function_statement_is_rejected(self):
        context = build_engineering_writer_context(self.root, task())
        bad = (
            "def target(self, value: int) -> int:\n"
            "    return value\n\n"
            "FLAG = True\n"
        )
        with self.assertRaises(EngineeringScopedEditError):
            materialize_engineering_edits(
                self.root,
                {"src/demo.py": ScopedPythonSymbolEdit("target", bad)},
                context,
                max_edit_file_bytes=256 * 1024,
                max_patch_bytes=512 * 1024,
            )

    def test_public_helper_is_rejected(self):
        context = build_engineering_writer_context(self.root, task())
        bad = """def target(self, value: int) -> int:
    return helper(value)

def helper(value):
    return value
"""
        with self.assertRaises(EngineeringScopedEditError):
            materialize_engineering_edits(
                self.root,
                {"src/demo.py": ScopedPythonSymbolEdit("target", bad)},
                context,
                max_edit_file_bytes=256 * 1024,
                max_patch_bytes=512 * 1024,
            )

    def test_stale_source_is_rejected_after_context_capture(self):
        context = build_engineering_writer_context(self.root, task())
        (self.root / "src" / "demo.py").write_text(
            SOURCE + "\n# changed\n",
            encoding="utf-8",
        )
        with self.assertRaises(EngineeringScopedEditError):
            materialize_engineering_edits(
                self.root,
                {"src/demo.py": ScopedPythonSymbolEdit(
                    "target",
                    "def target(self, value: int) -> int:\n    return value\n",
                )},
                context,
                max_edit_file_bytes=256 * 1024,
                max_patch_bytes=512 * 1024,
            )

    def test_legacy_full_file_edit_remains_supported_without_scope(self):
        generic = {
            "task_id": "legacy",
            "instruction": "Make the requested bounded source edit.",
            "target_paths": ["src/demo.py"],
            "language_hint": "python",
            "success_criteria": {},
            "diagnostics": [],
        }
        changed = SOURCE.replace('return "ok"', 'return "still-ok"')
        model = FixtureModel({
            "summary": "Legacy full file replacement.",
            "edits": [{
                "path": "src/demo.py",
                "content": changed,
                "reason": "Compatibility check.",
            }],
        })
        writer = EngineeringResearchBackedWriter(self.root, model)
        edits = writer.propose_text_edits(generic)
        self.assertEqual(edits["src/demo.py"], changed)
        self.assertIsNone(model.payloads[0]["scoped_edit"])


if __name__ == "__main__":
    unittest.main()
