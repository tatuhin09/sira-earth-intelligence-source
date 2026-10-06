from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.code_writer import CodeModelBatch
from sira.engineering_writer import (
    EngineeringResearchBackedWriter,
    EngineeringWriterOutputError,
)


SOURCE = """class Demo:
    def target(self, value: int) -> int:
        if value > 0:
            return value
        return 0

    def untouched(self) -> str:
        return "keep-me"
"""


def scoped_task():
    return {
        "task_id": "scoped-response-boundary-v053",
        "instruction": (
            "Improve src/demo.py:target with the smallest "
            "behavior-preserving change. Primary symbol: target."
        ),
        "target_paths": ["src/demo.py"],
        "target": {"path": "src/demo.py", "symbol": "target"},
        "language_hint": "python",
        "success_criteria": {},
        "diagnostics": [],
    }


class MissingSymbolModel:
    name = "fixture_missing_symbol"
    model_id = "fixture"

    def generate_patch(self, payload, schema):
        return CodeModelBatch({
            "summary": "Return the supplied target block.",
            "edits": [{
                "path": "src/demo.py",
                "content": (
                    "def target(self, value: int) -> int:\n"
                    "    return value if value > 0 else 0\n"
                ),
                "reason": "Reduce branches.",
            }],
        })


class SchemaCaptureModel:
    name = "fixture_schema_capture"
    model_id = "fixture"

    def __init__(self):
        self.schemas = []

    def generate_patch(self, payload, schema):
        self.schemas.append(schema)
        return CodeModelBatch({
            "summary": "Return one authorized scoped edit.",
            "edits": [{
                "path": "src/demo.py",
                "symbol": "target",
                "content": (
                    "def target(self, value: int) -> int:\n"
                    "    return value if value > 0 else 0\n"
                ),
                "reason": "Reduce branches.",
            }],
        })


class EngineeringScopedResponseBoundaryV053Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "demo.py").write_text(
            SOURCE,
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_scoped_response_without_symbol_is_rejected_before_materialization(self):
        writer = EngineeringResearchBackedWriter(
            self.root,
            MissingSymbolModel(),
        )

        with self.assertRaises(EngineeringWriterOutputError) as caught:
            writer.propose_text_edits(scoped_task())

        self.assertEqual(
            caught.exception.code,
            "scoped_engineering_output_must_use_authorized_symbol",
        )
        self.assertEqual(
            (self.root / "src" / "demo.py").read_text(encoding="utf-8"),
            SOURCE,
        )

    def test_scoped_request_schema_requires_symbol(self):
        model = SchemaCaptureModel()
        writer = EngineeringResearchBackedWriter(self.root, model)

        writer.propose_text_edits(scoped_task())

        required = model.schemas[0]["properties"]["edits"]["items"][
            "required"
        ]
        self.assertIn("symbol", required)


if __name__ == "__main__":
    unittest.main()
