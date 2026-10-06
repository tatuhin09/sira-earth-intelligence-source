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
        "task_id": "target-only-scope-v054",
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


class SiblingHelperModel:
    name = "fixture_sibling_helper"
    model_id = "fixture"

    def generate_patch(self, payload, schema):
        return CodeModelBatch({
            "summary": "Extract a sibling helper.",
            "edits": [{
                "path": "src/demo.py",
                "symbol": "target",
                "content": (
                    "def target(self, value: int) -> int:\n"
                    "    return self._normalize(value)\n\n"
                    "def _normalize(self, value: int) -> int:\n"
                    "    return value if value > 0 else 0\n"
                ),
                "reason": "Reduce branches.",
            }],
        })


class EngineeringTargetOnlyScopeV054Tests(unittest.TestCase):
    def test_scoped_writer_rejects_new_sibling_helper_before_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "demo.py").write_text(
                SOURCE,
                encoding="utf-8",
            )
            writer = EngineeringResearchBackedWriter(
                root,
                SiblingHelperModel(),
            )

            with self.assertRaises(EngineeringWriterOutputError) as caught:
                writer.propose_text_edits(scoped_task())

        self.assertEqual(
            caught.exception.code,
            "scoped_edit_block_must_contain_target_definition_only",
        )


if __name__ == "__main__":
    unittest.main()
