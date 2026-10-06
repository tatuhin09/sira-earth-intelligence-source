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
    prepare_engineering_edit_candidate,
)


class EngineeringEditingTests(unittest.TestCase):
    def write(self, root: Path, relative: str, content: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def make_multilanguage_project(self, root: Path) -> None:
        self.write(
            root,
            "package.json",
            '{"scripts":{"test":"vitest","build":"tsc"},'
            '"devDependencies":{"typescript":"1","vitest":"1"}}',
        )
        self.write(root, "tsconfig.json", "{}")
        self.write(root, "src/app.ts", "export const value: number = 1;\n")
        self.write(root, "src/helper.js", "export const helper = 1;\n")
        self.write(root, "go.mod", "module example/project\n")
        self.write(root, "cmd/app/main.go", "package main\nfunc main() {}\n")

    def test_policy_detects_languages_and_expands_safe_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            self.make_multilanguage_project(root)
            policy = build_engineering_edit_policy(root)

        self.assertEqual(policy["status"], "ready")
        self.assertIn("typescript", policy["editable_languages"])
        self.assertIn("javascript", policy["editable_languages"])
        self.assertIn("go", policy["editable_languages"])
        self.assertFalse(policy["main_tree_editing_allowed"])
        self.assertFalse(policy["promotion_authorized"])

    def test_candidate_copy_excludes_secrets_vendor_build_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            candidate = base / "candidate"
            root.mkdir()
            self.make_multilanguage_project(root)

            self.write(root, ".env", "TOKEN=secret\n")
            self.write(root, "node_modules/pkg/index.ts", "export const bad=1\n")
            self.write(root, "build/generated.go", "package build\n")

            outside = base / "outside.ts"
            outside.write_text("export const outside=1\n", encoding="utf-8")
            (root / "src" / "linked.ts").symlink_to(outside)

            manifest = prepare_engineering_candidate_workspace(
                root, candidate
            )

            self.assertTrue((candidate / "src/app.ts").is_file())
            self.assertTrue((candidate / "package.json").is_file())
            self.assertTrue((candidate / "go.mod").is_file())
            self.assertFalse((candidate / ".env").exists())
            self.assertFalse((candidate / "node_modules").exists())
            self.assertFalse((candidate / "build").exists())
            self.assertFalse((candidate / "src/linked.ts").exists())
            self.assertFalse(manifest["symlinks_copied"])
            self.assertFalse(manifest["promotion_allowed"])

    def test_multilanguage_edits_apply_only_inside_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            candidate = base / "candidate"
            root.mkdir()
            self.make_multilanguage_project(root)

            policy = build_engineering_edit_policy(root)
            prepare_engineering_candidate_workspace(
                root, candidate, policy=policy
            )
            result = ValidatedEngineeringCandidateEditor(
                policy
            ).apply_text_edits(
                candidate,
                {
                    "src/app.ts": "export const value: number = 2;\n",
                    "cmd/app/main.go": "package main\nfunc main(){println(2)}\n",
                    "tests/new.test.ts": "export const testValue = 1;\n",
                },
            )

            self.assertEqual(
                set(result["file_languages"].values()),
                {"typescript", "go"},
            )
            self.assertIn("tests/new.test.ts", result["files_changed"])
            self.assertTrue(result["verification_required"])
            self.assertFalse(result["promotion_authorized"])
            self.assertIn(
                "= 1;",
                (root / "src/app.ts").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "= 2;",
                (candidate / "src/app.ts").read_text(encoding="utf-8"),
            )

    def test_manifest_foreign_language_and_unsafe_roots_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            candidate = base / "candidate"
            root.mkdir()
            self.make_multilanguage_project(root)
            policy = build_engineering_edit_policy(root)
            prepare_engineering_candidate_workspace(
                root, candidate, policy=policy
            )
            editor = ValidatedEngineeringCandidateEditor(policy)

            bad = {
                "package.json": "{}",
                "src/new.rs": "fn main() {}\n",
                ".github/workflow.ts": "export const x=1\n",
                "scripts/deploy.ts": "export const x=1\n",
                "src/sira/runtime.py": "print('x')\n",
            }
            for relative, content in bad.items():
                with self.subTest(relative=relative):
                    with self.assertRaises(
                        EngineeringCandidateEditError
                    ):
                        editor.apply_text_edits(
                            candidate, {relative: content}
                        )

    def test_setup_py_and_symlink_targets_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            candidate = base / "candidate"
            root.mkdir()
            self.write(root, "src/app.py", "VALUE = 1\n")
            self.write(root, "setup.py", "from setuptools import setup\n")
            policy = build_engineering_edit_policy(root)
            prepare_engineering_candidate_workspace(
                root, candidate, policy=policy
            )
            editor = ValidatedEngineeringCandidateEditor(policy)

            with self.assertRaises(EngineeringCandidateEditError):
                editor.apply_text_edits(
                    candidate,
                    {"setup.py": "print('dependency mutation')\n"},
                )

            outside = base / "outside.py"
            outside.write_text("VALUE = 8\n", encoding="utf-8")
            alias = candidate / "src" / "alias.py"
            alias.symlink_to(outside)
            with self.assertRaises(EngineeringCandidateEditError):
                editor.apply_text_edits(
                    candidate,
                    {"src/alias.py": "VALUE = 9\n"},
                )

    def test_candidate_must_be_outside_main_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            self.write(root, "src/app.py", "VALUE = 1\n")
            with self.assertRaises(EngineeringCandidateEditError):
                prepare_engineering_candidate_workspace(
                    root,
                    root / "candidate",
                )

    def test_convenience_flow_plans_verification_but_does_not_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            workspace = base / "workspace"
            root.mkdir()
            self.write(root, "go.mod", "module example/project\n")
            self.write(root, "main.go", "package main\nfunc main() {}\n")

            result = prepare_engineering_edit_candidate(
                root,
                workspace,
                {"main.go": "package main\nfunc main(){println(2)}\n"},
            )

            self.assertEqual(result["status"], "candidate_prepared")
            self.assertIn("main.go", result["verification_plans"])
            plan = result["verification_plans"]["main.go"]
            self.assertEqual(plan["target_language"], "go")
            self.assertFalse(result["verification_executed"])
            self.assertFalse(result["main_tree_modified"])
            self.assertFalse(result["promotion_authorized"])


if __name__ == "__main__":
    unittest.main()
