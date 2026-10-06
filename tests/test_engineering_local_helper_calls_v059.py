"""Reject direct, provably invalid local helper calls before verification."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_scoped_edit import (
    EngineeringScopedEditError,
    ScopedPythonSymbolEdit,
    materialize_engineering_edits,
)
from sira.engineering_writer import build_engineering_writer_context


SOURCE = '''def _base_cluster_key(*, kind, category, capability, provider):
    return "|".join((kind, category, capability, provider or ""))


class MemoryStore:
    @staticmethod
    def _rebuild_clusters(conn):
        row = conn.execute("SELECT * FROM memories").fetchone()
        return _base_cluster_key(
            kind=row["kind"], category=row["category"],
            capability=row["capability"], provider=row["provider"],
        )
'''


class EngineeringLocalHelperCallsV059Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        path = self.root / "src/sira/memory.py"
        path.parent.mkdir(parents=True)
        path.write_text(SOURCE, encoding="utf-8")
        self.context = build_engineering_writer_context(self.root, {
            "task_id": "a11-positional-helper-replay",
            "instruction": "Refactor only _rebuild_clusters without changing behavior.",
            "target_paths": ["src/sira/memory.py"],
            "target": {"path": "src/sira/memory.py", "symbol": "_rebuild_clusters"},
            "language_hint": "python", "success_criteria": {}, "diagnostics": [],
        })

    def materialize(self, replacement):
        return materialize_engineering_edits(
            self.root,
            {"src/sira/memory.py": ScopedPythonSymbolEdit(
                "_rebuild_clusters", replacement,
            )},
            self.context,
            max_edit_file_bytes=256 * 1024,
            max_patch_bytes=512 * 1024,
        )

    def test_a11_positional_call_rejected_before_candidate_copy(self):
        replacement = '''@staticmethod
def _rebuild_clusters(conn):
    row = conn.execute("SELECT * FROM memories").fetchone()
    return _base_cluster_key(row["kind"], row["category"],
                             row["capability"], row["provider"])
'''
        with self.assertRaisesRegex(EngineeringScopedEditError, "positional arguments"):
            self.materialize(replacement)
        self.assertEqual((self.root / "src/sira/memory.py").read_text(), SOURCE)

    def test_valid_keyword_call_remains_candidate_only(self):
        replacement = '''@staticmethod
def _rebuild_clusters(conn):
    row = conn.execute("SELECT * FROM memories").fetchone()
    return _base_cluster_key(kind=row["kind"], category=row["category"],
                             capability=row["capability"], provider=row["provider"])
'''
        result = self.materialize(replacement)
        self.assertIn("capability=row", result["src/sira/memory.py"])
        self.assertEqual((self.root / "src/sira/memory.py").read_text(), SOURCE)

    def test_shadowed_helper_parameter_is_not_mistaken_for_module_helper(self):
        source = SOURCE.replace("def _rebuild_clusters(conn):", "def _rebuild_clusters(conn, _base_cluster_key):")
        (self.root / "src/sira/memory.py").write_text(source, encoding="utf-8")
        task = dict(self.context["task"])
        task["target"] = {"path": "src/sira/memory.py", "symbol": "_rebuild_clusters"}
        self.context = build_engineering_writer_context(self.root, task)
        replacement = '''@staticmethod
def _rebuild_clusters(conn, _base_cluster_key):
    return _base_cluster_key(1, 2, 3, 4)
'''
        result = self.materialize(replacement)
        self.assertIn("_base_cluster_key(1, 2, 3, 4)", result["src/sira/memory.py"])

    def test_module_rebinding_leaves_dynamic_helper_calls_to_verification(self):
        source = SOURCE.replace(
            "\n\nclass MemoryStore:",
            "\n_base_cluster_key = lambda *args: 'rebound'\n\nclass MemoryStore:",
        )
        (self.root / "src/sira/memory.py").write_text(source, encoding="utf-8")
        task = dict(self.context["task"])
        task["target"] = {"path": "src/sira/memory.py", "symbol": "_rebuild_clusters"}
        self.context = build_engineering_writer_context(self.root, task)
        replacement = '''@staticmethod
def _rebuild_clusters(conn):
    return _base_cluster_key(1, 2, 3, 4)
'''
        result = self.materialize(replacement)
        self.assertIn("_base_cluster_key(1, 2, 3, 4)", result["src/sira/memory.py"])


if __name__ == "__main__":
    unittest.main()
