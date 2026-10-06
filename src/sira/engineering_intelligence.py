"""Read-only project/language intelligence and safe verification planning.

v1.8A detects engineering stacks without executing project code, installing
packages, accessing the network, or changing SIRA's self-modification boundary.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib
from typing import Any, Callable, Mapping

from .models import utc_now

ENGINEERING_POLICY_VERSION = 1
MAX_SCAN_FILES = 5000
MAX_MANIFEST_BYTES = 512 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024

_IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".idea", ".vscode",
    ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    "node_modules", "dist", "build", "coverage", ".next",
    ".dart_tool", ".gradle", "target", "vendor",
    "runs", "memory", "runtime", "improvements",
})
_LANGUAGE_SUFFIXES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".dart": "dart",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".rs": "rust",
    ".go": "go",
    ".sql": "sql",
}
_LANGUAGE_ORDER = (
    "python", "typescript", "javascript", "dart", "java",
    "cpp", "c", "rust", "go", "sql",
)
_TOOL_NAMES = (
    "python", "pytest", "node", "npm", "pnpm", "yarn", "bun",
    "dart", "flutter", "java", "javac", "mvn", "gradle",
    "cmake", "ctest", "make", "gcc", "g++", "cargo", "rustc",
    "go", "sqlfluff",
)


def _bounded_text(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _manifest_paths(root: Path) -> list[str]:
    names = (
        "pyproject.toml", "requirements.txt", "requirements-dev.txt",
        "setup.py", "setup.cfg", "tox.ini", "pytest.ini",
        "package.json", "package-lock.json", "pnpm-lock.yaml",
        "yarn.lock", "bun.lockb", "bun.lock", "tsconfig.json",
        "pubspec.yaml", "pubspec.lock",
        "pom.xml", "build.gradle", "build.gradle.kts",
        "settings.gradle", "settings.gradle.kts",
        "CMakeLists.txt", "Makefile", "Cargo.toml", "Cargo.lock",
        "go.mod", "go.sum", ".sqlfluff", "sqlfluff.toml",
    )
    return [name for name in names if (root / name).is_file() and not (root / name).is_symlink()]


def _source_counts(root: Path) -> tuple[Counter[str], int, int]:
    counts: Counter[str] = Counter()
    scanned = 0
    skipped = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        dirs[:] = [
            name for name in dirs
            if name not in _IGNORED_DIRS
            and not (current_path / name).is_symlink()
        ]
        for name in sorted(names):
            if scanned >= MAX_SCAN_FILES:
                return counts, scanned, skipped
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                skipped += 1
                continue
            language = _LANGUAGE_SUFFIXES.get(path.suffix.casefold())
            if language is None:
                continue
            try:
                if path.stat().st_size > MAX_SOURCE_BYTES:
                    skipped += 1
                    continue
            except OSError:
                skipped += 1
                continue
            scanned += 1
            counts[language] += 1
    return counts, scanned, skipped


def _json_file(root: Path, name: str) -> dict[str, Any]:
    text = _bounded_text(root / name)
    if text is None:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _toml_file(root: Path, name: str) -> dict[str, Any]:
    text = _bounded_text(root / name)
    if text is None:
        return {}
    try:
        value = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _flatten_dependency_names(package: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        rows = package.get(field)
        if isinstance(rows, Mapping):
            for key in rows:
                if isinstance(key, str):
                    names.add(key.casefold())
    return names


def _python_dependency_text(root: Path, pyproject: Mapping[str, Any]) -> str:
    chunks: list[str] = []
    project = pyproject.get("project")
    if isinstance(project, Mapping):
        deps = project.get("dependencies")
        if isinstance(deps, list):
            chunks.extend(str(item) for item in deps)
        optional = project.get("optional-dependencies")
        if isinstance(optional, Mapping):
            for rows in optional.values():
                if isinstance(rows, list):
                    chunks.extend(str(item) for item in rows)
    tool = pyproject.get("tool")
    if isinstance(tool, Mapping):
        poetry = tool.get("poetry")
        if isinstance(poetry, Mapping):
            deps = poetry.get("dependencies")
            if isinstance(deps, Mapping):
                chunks.extend(str(key) for key in deps.keys())
    for name in ("requirements.txt", "requirements-dev.txt"):
        text = _bounded_text(root / name)
        if text:
            chunks.append(text)
    return "\n".join(chunks).casefold()


def _tool_snapshot(root: Path, resolver: Callable[[str], str | None]) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for name in _TOOL_NAMES:
        if name == "python":
            path = sys.executable
        else:
            try:
                path = resolver(name)
            except (OSError, ValueError, TypeError):
                path = None
        snapshot[name] = {"available": bool(path), "path": str(path) if path else None}
    for wrapper, tool in (("mvnw", "maven_wrapper"), ("gradlew", "gradle_wrapper")):
        path = root / wrapper
        available = path.is_file() and not path.is_symlink() and os.access(path, os.X_OK)
        snapshot[tool] = {"available": available, "path": f"./{wrapper}" if available else None}
    return snapshot


def _package_manager(root: Path) -> list[str]:
    managers = []
    if (root / "pnpm-lock.yaml").is_file():
        managers.append("pnpm")
    elif (root / "yarn.lock").is_file():
        managers.append("yarn")
    elif (root / "bun.lockb").is_file() or (root / "bun.lock").is_file():
        managers.append("bun")
    elif (root / "package-lock.json").is_file() or (root / "package.json").is_file():
        managers.append("npm")

    if (root / "uv.lock").is_file():
        managers.append("uv")
    elif (root / "poetry.lock").is_file():
        managers.append("poetry")
    elif any((root / name).is_file() for name in ("pyproject.toml", "requirements.txt", "setup.py")):
        managers.append("pip")

    if (root / "pubspec.yaml").is_file():
        managers.append("pub")
    if (root / "Cargo.toml").is_file():
        managers.append("cargo")
    if (root / "go.mod").is_file():
        managers.append("go_modules")
    if (root / "pom.xml").is_file():
        managers.append("maven")
    if any((root / name).is_file() for name in ("build.gradle", "build.gradle.kts")):
        managers.append("gradle")
    return sorted(set(managers))


def _detect_stacks(root: Path, counts: Counter[str], manifests: list[str]) -> tuple[list[str], list[str], list[str]]:
    package = _json_file(root, "package.json")
    package_deps = _flatten_dependency_names(package)
    pyproject = _toml_file(root, "pyproject.toml")
    pydeps = _python_dependency_text(root, pyproject)
    pubspec = (_bounded_text(root / "pubspec.yaml") or "").casefold()
    pom = (_bounded_text(root / "pom.xml") or "").casefold()
    gradle = "\n".join(_bounded_text(root / name) or "" for name in ("build.gradle", "build.gradle.kts")).casefold()

    frameworks: set[str] = set()
    build_systems: set[str] = set()
    test_runners: set[str] = set()

    if "pyproject.toml" in manifests:
        build_systems.add("pyproject")
        tool = pyproject.get("tool")
        if isinstance(tool, Mapping) and isinstance(tool.get("pytest"), Mapping):
            test_runners.add("pytest")
    if "pytest.ini" in manifests or "pytest" in pydeps:
        test_runners.add("pytest")
    if counts["python"] and (root / "tests").is_dir():
        test_runners.add("unittest")
    for marker, framework in (("django", "django"), ("fastapi", "fastapi"), ("flask", "flask")):
        if marker in pydeps:
            frameworks.add(framework)

    if "package.json" in manifests:
        build_systems.add("package_scripts")
        scripts = package.get("scripts")
        if isinstance(scripts, Mapping) and isinstance(scripts.get("test"), str):
            test_runners.add("package_test_script")
    for marker, framework in (
        ("next", "nextjs"), ("react", "react"), ("vue", "vue"),
        ("@angular/core", "angular"), ("svelte", "svelte"), ("express", "express"),
    ):
        if marker in package_deps:
            frameworks.add(framework)
    for runner in ("vitest", "jest", "mocha", "ava"):
        if runner in package_deps:
            test_runners.add(runner)
    if "tsconfig.json" in manifests:
        build_systems.add("typescript")

    if "pubspec.yaml" in manifests:
        build_systems.add("pub")
        if re.search(r"(?m)^\s*flutter\s*:", pubspec) or "sdk: flutter" in pubspec:
            frameworks.add("flutter")
            test_runners.add("flutter_test")
        elif counts["dart"]:
            test_runners.add("dart_test")

    if "pom.xml" in manifests:
        build_systems.add("maven")
        test_runners.add("maven_test")
        if "spring-boot" in pom:
            frameworks.add("spring_boot")
    if "build.gradle" in manifests or "build.gradle.kts" in manifests:
        build_systems.add("gradle")
        test_runners.add("gradle_test")
        if "org.springframework.boot" in gradle:
            frameworks.add("spring_boot")

    if "CMakeLists.txt" in manifests:
        build_systems.add("cmake")
        test_runners.add("ctest")
    if "Makefile" in manifests:
        build_systems.add("make")
    if "Cargo.toml" in manifests:
        build_systems.add("cargo")
        test_runners.add("cargo_test")
    if "go.mod" in manifests:
        build_systems.add("go")
        test_runners.add("go_test")
    if ".sqlfluff" in manifests or "sqlfluff.toml" in manifests:
        test_runners.add("sqlfluff")

    return sorted(frameworks), sorted(build_systems), sorted(test_runners)


def inspect_project(root: Path, *, tool_resolver: Callable[[str], str | None] = shutil.which) -> dict[str, Any]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("project root must exist")

    counts, scanned, skipped = _source_counts(root)
    manifests = _manifest_paths(root)
    frameworks, build_systems, test_runners = _detect_stacks(root, counts, manifests)
    package_managers = _package_manager(root)
    tools = _tool_snapshot(root, tool_resolver)

    language_rows = [
        {"language": language, "file_count": int(counts[language])}
        for language in _LANGUAGE_ORDER
        if counts[language] > 0
    ]
    language_rows.sort(key=lambda row: (-int(row["file_count"]), _LANGUAGE_ORDER.index(str(row["language"]))))
    primary = language_rows[0]["language"] if language_rows else None

    return {
        "schema": "sira.engineering_project_profile.v1",
        "policy_version": ENGINEERING_POLICY_VERSION,
        "created_at": utc_now(),
        "root": str(root),
        "primary_language": primary,
        "languages": language_rows,
        "frameworks": frameworks,
        "build_systems": build_systems,
        "package_managers": package_managers,
        "test_runners": test_runners,
        "manifests": manifests,
        "tool_availability": tools,
        "scan": {"source_files_scanned": scanned, "files_skipped": skipped, "max_files": MAX_SCAN_FILES},
        "network_requests": 0,
        "processes_executed": 0,
        "packages_installed": 0,
        "paid_spending": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }


def language_for_path(path: str) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    return _LANGUAGE_SUFFIXES.get(Path(path).suffix.casefold())


def _package_script(root: Path, name: str) -> bool:
    package = _json_file(root, "package.json")
    scripts = package.get("scripts")
    return isinstance(scripts, Mapping) and isinstance(scripts.get(name), str) and bool(scripts[name].strip())


def _entry(command_id: str, kind: str, argv: list[str], *, available: bool, language: str,
           env: Mapping[str, str] | None = None, writes_generated_artifacts: bool = False) -> dict[str, Any]:
    return {
        "command_id": command_id,
        "kind": kind,
        "language": language,
        "argv": argv,
        "cwd": ".",
        "env": dict(env or {}),
        "available": bool(available),
        "candidate_only": True,
        "network_policy": "sandbox_network_must_be_disabled",
        "writes_generated_artifacts": bool(writes_generated_artifacts),
        "shell": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }


def plan_verification(root: Path, *, profile: Mapping[str, Any] | None = None,
                      target_path: str | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    if profile is None:
        profile = inspect_project(root)
    if not isinstance(profile, Mapping):
        raise ValueError("engineering profile must be a mapping")

    tools = profile.get("tool_availability")
    tools = tools if isinstance(tools, Mapping) else {}
    target_language = language_for_path(target_path) if target_path else None
    detected = {row.get("language") for row in profile.get("languages", []) if isinstance(row, Mapping)}
    languages = {target_language} if target_language else set(detected)
    languages.discard(None)
    commands: list[dict[str, Any]] = []

    def available(name: str) -> bool:
        row = tools.get(name)
        return isinstance(row, Mapping) and row.get("available") is True

    runners = set(profile.get("test_runners", []))
    build_systems = set(profile.get("build_systems", []))
    managers = set(profile.get("package_managers", []))

    if "python" in languages:
        if "pytest" in runners:
            commands.append(_entry(
                "python.pytest", "test", [sys.executable, "-m", "pytest", "-q"],
                available=available("pytest"), language="python",
            ))
        if "unittest" in runners:
            commands.append(_entry(
                "python.unittest", "test",
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                available=True, language="python",
            ))

    if languages & {"javascript", "typescript"}:
        manager = next((name for name in ("pnpm", "yarn", "bun", "npm") if name in managers), "npm")
        manager_available = available(manager)
        for script, kind in (("lint", "lint"), ("test", "test"), ("build", "build")):
            if not _package_script(root, script):
                continue
            if manager == "npm":
                argv = ["npm", "test"] if script == "test" else ["npm", "run", script]
            elif manager in {"yarn", "pnpm"}:
                argv = [manager, script]
            else:
                argv = ["bun", "run", script]
            commands.append(_entry(
                f"node.{script}", kind, argv, available=manager_available,
                language="typescript" if "typescript" in languages else "javascript",
                writes_generated_artifacts=kind == "build",
            ))
        local_tsc = root / "node_modules" / ".bin" / "tsc"
        if "typescript" in languages and "typescript" in build_systems:
            commands.append(_entry(
                "typescript.typecheck", "typecheck",
                ["./node_modules/.bin/tsc", "--noEmit"],
                available=local_tsc.is_file() and not local_tsc.is_symlink(),
                language="typescript",
            ))

    if "dart" in languages:
        if "flutter" in set(profile.get("frameworks", [])):
            commands.extend([
                _entry("flutter.analyze", "lint", ["flutter", "analyze"], available=available("flutter"), language="dart"),
                _entry("flutter.test", "test", ["flutter", "test"], available=available("flutter"), language="dart"),
            ])
        else:
            commands.extend([
                _entry("dart.analyze", "lint", ["dart", "analyze"], available=available("dart"), language="dart"),
                _entry("dart.test", "test", ["dart", "test"], available=available("dart"), language="dart"),
            ])

    if "java" in languages:
        if "maven" in build_systems:
            wrapper = available("maven_wrapper")
            commands.append(_entry(
                "java.maven_test", "test",
                ["./mvnw", "-o", "test"] if wrapper else ["mvn", "-o", "test"],
                available=wrapper or available("mvn"), language="java", writes_generated_artifacts=True,
            ))
        elif "gradle" in build_systems:
            wrapper = available("gradle_wrapper")
            commands.append(_entry(
                "java.gradle_test", "test",
                ["./gradlew", "--offline", "test"] if wrapper else ["gradle", "--offline", "test"],
                available=wrapper or available("gradle"), language="java", writes_generated_artifacts=True,
            ))

    if languages & {"c", "cpp"} and "cmake" in build_systems:
        lang = "cpp" if "cpp" in languages else "c"
        commands.extend([
            _entry(
                "cmake.configure", "configure",
                ["cmake", "-S", ".", "-B", ".sira-build", "-DFETCHCONTENT_FULLY_DISCONNECTED=ON"],
                available=available("cmake"), language=lang, writes_generated_artifacts=True,
            ),
            _entry(
                "cmake.build", "build", ["cmake", "--build", ".sira-build"],
                available=available("cmake"), language=lang, writes_generated_artifacts=True,
            ),
            _entry(
                "cmake.test", "test",
                ["ctest", "--test-dir", ".sira-build", "--output-on-failure"],
                available=available("ctest"), language=lang, writes_generated_artifacts=True,
            ),
        ])

    if "rust" in languages and "cargo" in build_systems:
        commands.extend([
            _entry("rust.check", "typecheck", ["cargo", "check", "--offline"], available=available("cargo"), language="rust", writes_generated_artifacts=True),
            _entry("rust.test", "test", ["cargo", "test", "--offline"], available=available("cargo"), language="rust", writes_generated_artifacts=True),
        ])

    if "go" in languages and "go" in build_systems:
        commands.append(_entry(
            "go.test", "test", ["go", "test", "./..."],
            available=available("go"), language="go",
            env={"GOPROXY": "off", "GOSUMDB": "off"},
            writes_generated_artifacts=True,
        ))

    if "sql" in languages and "sqlfluff" in runners:
        commands.append(_entry(
            "sql.sqlfluff", "lint", ["sqlfluff", "lint", "."],
            available=available("sqlfluff"), language="sql",
        ))

    return {
        "schema": "sira.engineering_verification_plan.v1",
        "policy_version": ENGINEERING_POLICY_VERSION,
        "target_path": target_path,
        "target_language": target_language,
        "commands": commands,
        "command_count": len(commands),
        "available_command_count": sum(row["available"] for row in commands),
        "execution_performed": False,
        "install_performed": False,
        "network_requests": 0,
        "candidate_only_required": True,
        "network_disabled_required": True,
        "paid_spending": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }
