from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_editing import (
    EngineeringCandidateEditError,
    ValidatedEngineeringCandidateEditor,
    build_engineering_edit_policy,
    prepare_engineering_candidate_workspace,
)


class EngineeringVerificationSupportCopyV055Tests(unittest.TestCase):
    def write(self, root: Path, relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_safe_verification_support_is_copied_but_not_editable(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            candidate = base / "candidate"
            project.mkdir()

            self.write(project, "src/app.py", "VALUE = 1\n")
            support_files = {
                ".gitignore": "runtime/\nmemory/\n",
                "benchmarks/cases.json": "{}\n",
                "desktop/static/index.html": "<main>SIRA</main>\n",
                "desktop/static/styles.css": "main { color: blue; }\n",
                "desktop/assets/sira.svg": "<svg></svg>\n",
                "fixtures/settings.toml": "enabled = true\n",
                "fixtures/settings.yaml": "enabled: true\n",
                "docs/verification.md": "# Verification\n",
                "fixtures/expected.txt": "expected\n",
            }
            for relative, content in support_files.items():
                self.write(project, relative, content)

            self.write(project, "credentials.json", '{"token":"secret"}\n')
            self.write(project, ".private/fixture.json", "{}\n")
            self.write(project, "runtime/report.json", "{}\n")

            policy = build_engineering_edit_policy(project)
            prepare_engineering_candidate_workspace(
                project,
                candidate,
                policy=policy,
            )

            for relative in support_files:
                with self.subTest(relative=relative):
                    self.assertTrue((candidate / relative).is_file())

            self.assertFalse((candidate / "credentials.json").exists())
            self.assertFalse((candidate / ".private").exists())
            self.assertFalse((candidate / "runtime").exists())

            editor = ValidatedEngineeringCandidateEditor(policy)
            with self.assertRaises(EngineeringCandidateEditError):
                editor.apply_text_edits(
                    candidate,
                    {"benchmarks/cases.json": '{"weakened":true}\n'},
                )


if __name__ == "__main__":
    unittest.main()
