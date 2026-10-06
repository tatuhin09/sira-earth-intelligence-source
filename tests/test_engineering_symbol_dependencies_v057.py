"""The scoped writer must see local call interfaces without full helper bodies."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_scoped_edit import compact_engineering_model_context
from sira.engineering_writer import build_engineering_writer_context


class EngineeringSymbolDependenciesV057Tests(unittest.TestCase):
    def test_keyword_only_helper_interface_reaches_model_without_its_body(self):
        source = '''def _base_cluster_key(*, kind: str, category: str,
                      capability: str, provider: str | None) -> str:
    """PRIVATE_HELPER_TEXT_MUST_NOT_LEAK"""
    return kind + category + capability + (provider or "")


class MemoryStore:
    @staticmethod
    def _rebuild_clusters(conn):
        row = conn.execute("SELECT * FROM memories").fetchone()
        return _base_cluster_key(
            kind=row["kind"], category=row["category"],
            capability=row["capability"], provider=row["provider"],
        )
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "src/sira/memory.py"
            path.parent.mkdir(parents=True)
            path.write_text(source, encoding="utf-8")
            context = build_engineering_writer_context(root, {
                "task_id": "a11-helper-signature",
                "instruction": "Refactor only _rebuild_clusters while preserving behavior.",
                "target_paths": ["src/sira/memory.py"],
                "target": {"path": "src/sira/memory.py", "symbol": "_rebuild_clusters"},
                "language_hint": "python",
                "success_criteria": {},
                "diagnostics": [],
            })
            sent = compact_engineering_model_context(context)

        self.assertEqual(sent["scoped_edit"]["referenced_signatures"], [
            {"name": "_base_cluster_key",
             "signature": "def _base_cluster_key(*, kind, category, capability, provider)"},
        ])
        target = next(row for row in sent["files"] if row["path"] == "src/sira/memory.py")
        self.assertIn("def _rebuild_clusters(", target["content"])
        self.assertNotIn("PRIVATE_HELPER_TEXT_MUST_NOT_LEAK", repr(sent))


if __name__ == "__main__":
    unittest.main()
