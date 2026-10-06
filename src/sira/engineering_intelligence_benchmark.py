from pathlib import Path
import tempfile

from .engineering_intelligence import inspect_project, plan_verification
from .storage import RunStore, write_json


def _resolver(name: str) -> str:
    return f"/tool/{name}"


def _write(root: Path, rel: str, text: str):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def engineering_intelligence_benchmark(root: Path):
    root = Path(root).resolve()
    cases = []
    add = lambda case_id, passed: cases.append({"case_id": case_id, "passed": bool(passed)})

    with tempfile.TemporaryDirectory(prefix="sira-eng-") as tmp:
        base = Path(tmp)

        py = base / "python"
        _write(py, "pyproject.toml", '[project]\nname="x"\ndependencies=["pytest","fastapi"]\n')
        _write(py, "src/app.py", "print('x')\n")
        _write(py, "tests/test_app.py", "import unittest\n")
        pp = inspect_project(py, tool_resolver=_resolver)
        add("python_detected", pp["primary_language"] == "python")
        add("python_framework", "fastapi" in pp["frameworks"])
        add("python_tests", {"pytest", "unittest"} <= set(pp["test_runners"]))

        node = base / "node"
        _write(node, "package.json", '{"scripts":{"test":"vitest","build":"next build"},"dependencies":{"next":"1","react":"1"},"devDependencies":{"typescript":"1","vitest":"1"}}')
        _write(node, "pnpm-lock.yaml", "lockfileVersion: 9\n")
        _write(node, "tsconfig.json", "{}")
        _write(node, "src/page.tsx", "export default 1\n")
        np = inspect_project(node, tool_resolver=_resolver)
        add("next_typescript", np["primary_language"] == "typescript" and "nextjs" in np["frameworks"])
        add("pnpm_detected", "pnpm" in np["package_managers"])
        nplan = plan_verification(node, profile=np, target_path="src/page.tsx")
        add("node_plan", any(row["command_id"] == "node.test" for row in nplan["commands"]))

        flutter = base / "flutter"
        _write(flutter, "pubspec.yaml", "dependencies:\n  flutter:\n    sdk: flutter\n")
        _write(flutter, "lib/main.dart", "void main() {}\n")
        fp = inspect_project(flutter, tool_resolver=_resolver)
        add("flutter_detected", "flutter" in fp["frameworks"])

        java = base / "java"
        _write(java, "pom.xml", "<project><artifactId>x</artifactId></project>")
        _write(java, "src/main/java/A.java", "class A {}\n")
        jp = inspect_project(java, tool_resolver=_resolver)
        jplan = plan_verification(java, profile=jp, target_path="src/main/java/A.java")
        add("maven_offline", any("-o" in row["argv"] for row in jplan["commands"]))

        cpp = base / "cpp"
        _write(cpp, "CMakeLists.txt", "cmake_minimum_required(VERSION 3.20)\n")
        _write(cpp, "src/main.cpp", "int main(){return 0;}\n")
        cp = inspect_project(cpp, tool_resolver=_resolver)
        cplan = plan_verification(cpp, profile=cp, target_path="src/main.cpp")
        add("cmake_offline_guard", any("FETCHCONTENT_FULLY_DISCONNECTED=ON" in " ".join(row["argv"]) for row in cplan["commands"]))

        rust = base / "rust"
        _write(rust, "Cargo.toml", '[package]\nname="x"\nversion="0.1.0"\n')
        _write(rust, "src/lib.rs", "pub fn x() {}\n")
        rp = inspect_project(rust, tool_resolver=_resolver)
        rplan = plan_verification(rust, profile=rp, target_path="src/lib.rs")
        add("rust_offline", all("--offline" in row["argv"] for row in rplan["commands"]))

        go = base / "go"
        _write(go, "go.mod", "module example/x\n")
        _write(go, "main.go", "package main\n")
        gp = inspect_project(go, tool_resolver=_resolver)
        gplan = plan_verification(go, profile=gp, target_path="main.go")
        grow = next(row for row in gplan["commands"] if row["command_id"] == "go.test")
        add("go_network_off", grow["env"].get("GOPROXY") == "off")

        sql = base / "sql"
        _write(sql, ".sqlfluff", "[sqlfluff]\ndialect = postgres\n")
        _write(sql, "query.sql", "select 1;\n")
        sp = inspect_project(sql, tool_resolver=_resolver)
        splan = plan_verification(sql, profile=sp, target_path="query.sql")
        add("sql_lint", any(row["command_id"] == "sql.sqlfluff" for row in splan["commands"]))

        add("non_authoritative", pp["processes_executed"] == 0 and nplan["execution_performed"] is False and nplan["authority_granted"] is False and nplan["promotion_authorized"] is False)

    report = {
        "schema_version": 1,
        "kind": "engineering_intelligence_benchmark",
        "suite_id": "sira-engineering-intelligence-v1.8a",
        "passed": sum(row["passed"] for row in cases),
        "failed": sum(not row["passed"] for row in cases),
        "api_requests": 0,
        "cases": cases,
    }
    run = RunStore(root)
    path = run.path / "engineering-intelligence-benchmark.json"
    write_json(path, report)
    return path, report
