import json
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class SelfModificationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src" / "sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "src" / "sira" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "src" / "sira" / "runtime.py").write_text("PROTECTED = True\n", encoding="utf-8")
        (self.root / "src" / "sira" / "self_modification.py").write_text("BOUNDARY = True\n", encoding="utf-8")
        (self.root / "tests" / "test_feature.py").write_text("# test\n", encoding="utf-8")
        (self.root / "benchmarks" / "case.json").write_text("{}\n", encoding="utf-8")
        (self.root / "docs" / "note.md").write_text("note\n", encoding="utf-8")
        (self.root / "sira.py").write_text("print('sira')\n", encoding="utf-8")
        (self.root / "README.md").write_text("readme\n", encoding="utf-8")
        (self.root / ".env.example").write_text("SAFE_EXAMPLE=1\n", encoding="utf-8")
        for name in ("memory", "runtime", "runs", "improvements", ".git", ".sira_update_backups", ".cache"):
            path = self.root / name
            path.mkdir()
            (path / "secret.txt").write_text("must-not-copy\n", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=never-copy\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_workspace_copies_only_public_allowlist_and_writes_policy_manifest(self):
        from sira.self_modification import prepare_candidate_workspace

        destination = self.root / "scratch" / "candidate"
        result = prepare_candidate_workspace(self.root, destination)
        self.assertEqual(result["policy_version"], 1)
        self.assertTrue((destination / "src" / "sira" / "feature.py").is_file())
        self.assertTrue((destination / "sira.py").is_file())
        self.assertTrue((destination / "README.md").is_file())
        self.assertTrue((destination / ".env.example").is_file())
        for forbidden in (".env", "memory", "runtime", "runs", "improvements", ".git", ".sira_update_backups", ".cache"):
            self.assertFalse((destination / forbidden).exists(), forbidden)
        manifest = json.loads((destination.parent / "candidate_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["kind"], "candidate_workspace_manifest")
        self.assertIn("src/sira/runtime.py", manifest["protected_paths"])
        self.assertIn("src/sira", manifest["modifiable_roots"])

    def test_allowed_edit_changes_candidate_only_not_main_tree(self):
        from sira.self_modification import ValidatedCandidateEditor, prepare_candidate_workspace

        destination = self.root / "scratch" / "candidate"
        prepare_candidate_workspace(self.root, destination)
        editor = ValidatedCandidateEditor()
        result = editor.apply_text_edits(destination, {"src/sira/feature.py": "VALUE = 2\n"})
        self.assertEqual(result["files_changed"], ["src/sira/feature.py"])
        self.assertEqual((destination / "src" / "sira" / "feature.py").read_text(), "VALUE = 2\n")
        self.assertEqual((self.root / "src" / "sira" / "feature.py").read_text(), "VALUE = 1\n")

    def test_protected_absolute_traversal_binary_and_unapproved_paths_are_rejected(self):
        from sira.self_modification import CandidateEditError, ValidatedCandidateEditor, prepare_candidate_workspace

        destination = self.root / "scratch" / "candidate"
        prepare_candidate_workspace(self.root, destination)
        editor = ValidatedCandidateEditor()
        bad = {
            "src/sira/runtime.py": "x\n",
            "src/sira/cli.py": "x\n",
            "src/sira/self_modification.py": "x\n",
            "../escape.py": "x\n",
            "/tmp/escape.py": "x\n",
            "README.md": "x\n",
            "src/sira/new.py": "bad\x00binary",
        }
        for path, content in bad.items():
            with self.subTest(path=path):
                with self.assertRaises(CandidateEditError):
                    editor.apply_text_edits(destination, {path: content})

    def test_symlink_target_is_rejected(self):
        from sira.self_modification import CandidateEditError, ValidatedCandidateEditor, prepare_candidate_workspace

        destination = self.root / "scratch" / "candidate"
        prepare_candidate_workspace(self.root, destination)
        target = destination / "src" / "sira" / "outside.py"
        target.symlink_to(self.root / "README.md")
        with self.assertRaises(CandidateEditError):
            ValidatedCandidateEditor().apply_text_edits(destination, {"src/sira/outside.py": "x\n"})

    def test_candidate_root_symlink_is_rejected(self):
        from sira.self_modification import CandidateEditError, ValidatedCandidateEditor, prepare_candidate_workspace

        destination = self.root / "scratch" / "candidate"
        prepare_candidate_workspace(self.root, destination)
        alias = self.root / "candidate-alias"
        alias.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(CandidateEditError):
            ValidatedCandidateEditor().apply_text_edits(alias, {"src/sira/feature.py": "VALUE = 3\n"})


if __name__ == "__main__":
    unittest.main()
