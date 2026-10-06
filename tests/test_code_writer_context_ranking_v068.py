"""Keep context ranking bounded while simplifying its caller."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.autonomous_promotion import _branch_points_for_symbol
from sira.code_writer import MAX_CONTEXT_FILE_BYTES, build_project_context


class CodeWriterContextRankingTests(unittest.TestCase):
    def test_explicit_context_keeps_content_hash_and_bounded_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src/sira/reader.py"
            source.parent.mkdir(parents=True)
            source.write_text("def read():\n    return 42\n", encoding="utf-8")
            (root / "src/sira/runtime.py").write_text("SECRET = True\n", encoding="utf-8")
            (root / "src/sira/alias.py").symlink_to(source)
            docs = root / "docs"
            docs.mkdir()
            (docs / "large.md").write_bytes(b"x" * (MAX_CONTEXT_FILE_BYTES + 1))

            context = build_project_context(root, {
                "statement": "Improve src/sira/reader.py using its original source.",
                "memory_snapshot": {},
            })

            self.assertEqual(context["explicit_paths"], ["src/sira/reader.py"])
            self.assertEqual([row["path"] for row in context["files"]], ["src/sira/reader.py"])
            self.assertEqual(context["files"][0]["content"], "def read():\n    return 42\n")
            self.assertEqual(
                context["files"][0]["sha256"],
                hashlib.sha256(b"def read():\n    return 42\n").hexdigest(),
            )
            index = {row["path"]: row for row in context["available_context_index"]}
            self.assertEqual(index["docs/large.md"]["bytes"], MAX_CONTEXT_FILE_BYTES + 1)
            self.assertNotIn("src/sira/runtime.py", index)
            self.assertNotIn("src/sira/alias.py", index)

    def test_context_builder_meets_local_branch_goal(self):
        self.assertLessEqual(
            _branch_points_for_symbol(ROOT, "src/sira/code_writer.py", "build_project_context"),
            31,  # Current baseline 35; the existing opportunity goal removes at least four.
        )


if __name__ == "__main__":
    unittest.main()
