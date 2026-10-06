from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_intelligence import inspect_project, plan_verification


class EngineeringIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def resolver(name):
        return f"/tools/{name}"

    def test_python_profile_is_read_only_and_detects_frameworks(self):
        self.write("pyproject.toml", '[project]\nname="x"\ndependencies=["pytest","fastapi"]\n')
        self.write("src/app.py", "def app(): return 1\n")
        self.write("tests/test_app.py", "import unittest\n")
        profile = inspect_project(self.root, tool_resolver=self.resolver)
        self.assertEqual(profile["primary_language"], "python")
        self.assertIn("fastapi", profile["frameworks"])
        self.assertIn("pytest", profile["test_runners"])
        self.assertIn("unittest", profile["test_runners"])
        self.assertEqual(profile["processes_executed"], 0)
        self.assertEqual(profile["network_requests"], 0)
        self.assertFalse(profile["authority_granted"])

    def test_next_typescript_and_package_manager_are_detected(self):
        self.write(
            "package.json",
            '{"scripts":{"test":"vitest","build":"next build","lint":"eslint ."},'
            '"dependencies":{"next":"1","react":"1"},'
            '"devDependencies":{"typescript":"1","vitest":"1"}}',
        )
        self.write("pnpm-lock.yaml", "lockfileVersion: 9\n")
        self.write("tsconfig.json", "{}")
        self.write("src/page.tsx", "export default function Page(){return null}\n")
        profile = inspect_project(self.root, tool_resolver=self.resolver)
        self.assertEqual(profile["primary_language"], "typescript")
        self.assertIn("nextjs", profile["frameworks"])
        self.assertIn("pnpm", profile["package_managers"])
        plan = plan_verification(self.root, profile=profile, target_path="src/page.tsx")
        ids = {row["command_id"] for row in plan["commands"]}
        self.assertIn("node.test", ids)
        self.assertIn("node.build", ids)
        self.assertIn("node.lint", ids)
        self.assertFalse(plan["execution_performed"])

    def test_flutter_java_cpp_rust_go_sql_plans_are_offline_bounded(self):
        fixtures = [
            ("flutter", {"pubspec.yaml": "dependencies:\n  flutter:\n    sdk: flutter\n", "lib/main.dart": "void main() {}\n"}, "lib/main.dart", "flutter.test"),
            ("java", {"pom.xml": "<project></project>", "src/main/java/A.java": "class A {}\n"}, "src/main/java/A.java", "java.maven_test"),
            ("cpp", {"CMakeLists.txt": "cmake_minimum_required(VERSION 3.20)\n", "src/main.cpp": "int main(){return 0;}\n"}, "src/main.cpp", "cmake.configure"),
            ("rust", {"Cargo.toml": '[package]\nname="x"\nversion="0.1.0"\n', "src/lib.rs": "pub fn x() {}\n"}, "src/lib.rs", "rust.test"),
            ("go", {"go.mod": "module example/x\n", "main.go": "package main\n"}, "main.go", "go.test"),
            ("sql", {".sqlfluff": "[sqlfluff]\ndialect=postgres\n", "query.sql": "select 1;\n"}, "query.sql", "sql.sqlfluff"),
        ]
        for name, files, target, expected in fixtures:
            with self.subTest(stack=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    for rel, text in files.items():
                        path = root / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(text, encoding="utf-8")
                    profile = inspect_project(root, tool_resolver=self.resolver)
                    plan = plan_verification(root, profile=profile, target_path=target)
                    rows = {row["command_id"]: row for row in plan["commands"]}
                    self.assertIn(expected, rows)
                    for row in rows.values():
                        self.assertTrue(row["candidate_only"])
                        self.assertFalse(row["shell"])
                        self.assertFalse(row["promotion_authorized"])

    def test_vendor_and_symlink_trees_are_not_scanned(self):
        self.write("src/app.py", "print('x')\n")
        self.write("node_modules/pkg/index.ts", "export const bad = 1\n")
        with tempfile.TemporaryDirectory() as external_tmp:
            target = Path(external_tmp)
            (target / "hack.rs").write_text(
                "pub fn x() {}\n", encoding="utf-8"
            )
            (self.root / "linked").symlink_to(
                target, target_is_directory=True
            )
            profile = inspect_project(
                self.root, tool_resolver=self.resolver
            )
            languages = {
                row["language"] for row in profile["languages"]
            }
            self.assertEqual(languages, {"python"})


if __name__ == "__main__":
    unittest.main()
