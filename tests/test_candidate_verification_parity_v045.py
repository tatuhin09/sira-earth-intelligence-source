from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sira.self_modification import (
    CandidateEditError,
    MODIFIABLE_ROOTS,
    PUBLIC_COPY_ENTRIES,
    ValidatedCandidateEditor,
    _public_tree_digest,
    prepare_candidate_workspace,
)


class CandidateVerificationParityV045Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "main"
        self.root.mkdir()

        (self.root / "sira.py").write_text("# cli\n", encoding="utf-8")
        (self.root / "README.md").write_text("readme\n", encoding="utf-8")
        (self.root / ".env.example").write_text("KEY=\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("runtime/\n", encoding="utf-8")

        for directory in (
            "src/sira",
            "tests",
            "benchmarks",
            "docs",
            "desktop/static",
            "desktop/assets",
            "tools",
        ):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

        (self.root / "src/sira/example.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "tests/test_example.py").write_text("# test\n", encoding="utf-8")
        (self.root / "benchmarks/cases.json").write_text("{}\n", encoding="utf-8")
        (self.root / "docs/guide.md").write_text("guide\n", encoding="utf-8")
        (self.root / "desktop/static/app.js").write_text("const ok = true;\n", encoding="utf-8")
        (self.root / "desktop/assets/sira.svg").write_text("<svg></svg>\n", encoding="utf-8")
        (self.root / "tools/install_sira_desktop.py").write_text("# installer\n", encoding="utf-8")

        self.candidate = Path(self.tmp.name) / "candidate"

    def tearDown(self):
        self.tmp.cleanup()

    def test_verification_support_entries_are_copied(self):
        manifest = prepare_candidate_workspace(self.root, self.candidate)

        self.assertIn("desktop", PUBLIC_COPY_ENTRIES)
        self.assertIn("tools", PUBLIC_COPY_ENTRIES)
        self.assertIn(".gitignore", PUBLIC_COPY_ENTRIES)

        for relative in (
            "desktop/static/app.js",
            "desktop/assets/sira.svg",
            "tools/install_sira_desktop.py",
            ".gitignore",
        ):
            self.assertTrue((self.candidate / relative).is_file(), relative)

        self.assertFalse(manifest["promotion_allowed"])
        self.assertFalse(manifest["credentials_copied"])

    def test_support_entries_remain_non_modifiable(self):
        prepare_candidate_workspace(self.root, self.candidate)
        editor = ValidatedCandidateEditor()

        self.assertNotIn("desktop", MODIFIABLE_ROOTS)
        self.assertNotIn("tools", MODIFIABLE_ROOTS)

        for path in (
            "desktop/static/app.js",
            "tools/install_sira_desktop.py",
            ".gitignore",
        ):
            with self.assertRaises(CandidateEditError):
                editor.apply_text_edits(
                    self.candidate,
                    {path: "changed\n"},
                )

    def test_public_digest_binds_verification_support_files(self):
        before = _public_tree_digest(self.root)
        (self.root / "desktop/static/app.js").write_text(
            "const ok = false;\n",
            encoding="utf-8",
        )
        after = _public_tree_digest(self.root)
        self.assertNotEqual(before, after)

    def test_normal_docs_edit_is_still_allowed(self):
        prepare_candidate_workspace(self.root, self.candidate)
        report = ValidatedCandidateEditor().apply_text_edits(
            self.candidate,
            {"docs/writer_live_check_candidate.md": "SIRA_WRITER_LIVE_CHECK_OK\n"},
        )
        self.assertEqual(
            report["files_changed"],
            ["docs/writer_live_check_candidate.md"],
        )


if __name__ == "__main__":
    unittest.main()
