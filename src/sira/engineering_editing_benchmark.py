from pathlib import Path
import tempfile

from .engineering_editing import (
    EngineeringCandidateEditError,
    ValidatedEngineeringCandidateEditor,
    build_engineering_edit_policy,
    prepare_engineering_candidate_workspace,
)
from .storage import RunStore, write_json


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def engineering_editing_benchmark(root: Path):
    cases = []
    add = lambda case_id, passed: cases.append(
        {"case_id": case_id, "passed": bool(passed)}
    )

    with tempfile.TemporaryDirectory(prefix="sira-editing-") as tmp:
        base = Path(tmp)
        project = base / "project"
        candidate = base / "candidate"
        project.mkdir()

        _write(
            project,
            "package.json",
            '{"scripts":{"test":"vitest"},"devDependencies":{"typescript":"1"}}',
        )
        _write(project, "tsconfig.json", "{}")
        _write(project, "src/app.ts", "export const n: number = 1;\n")
        _write(project, "go.mod", "module example/x\n")
        _write(project, "cmd/app/main.go", "package main\nfunc main() {}\n")
        _write(project, ".env", "SECRET=do-not-copy\n")
        _write(project, "node_modules/pkg/index.ts", "export const bad=1;\n")
        _write(project, "build/generated.go", "package build\n")

        outside = base / "outside.ts"
        outside.write_text("export const outside=1;\n", encoding="utf-8")
        (project / "src" / "linked.ts").symlink_to(outside)

        policy = build_engineering_edit_policy(project)
        add("policy_ready", policy["status"] == "ready")
        add(
            "typescript_family_expanded",
            {"typescript", "javascript"}
            <= set(policy["editable_languages"]),
        )
        add("go_detected", "go" in policy["editable_languages"])

        manifest = prepare_engineering_candidate_workspace(
            project, candidate, policy=policy
        )
        add("source_copied", (candidate / "src/app.ts").is_file())
        add("manifest_copied", (candidate / "package.json").is_file())
        add("env_excluded", not (candidate / ".env").exists())
        add(
            "vendor_build_excluded",
            not (candidate / "node_modules").exists()
            and not (candidate / "build").exists(),
        )
        add("symlink_excluded", not (candidate / "src/linked.ts").exists())

        editor = ValidatedEngineeringCandidateEditor(policy)
        result = editor.apply_text_edits(
            candidate,
            {
                "src/app.ts": "export const n: number = 2;\n",
                "cmd/app/main.go": "package main\nfunc main(){println(2)}\n",
            },
        )
        add(
            "multi_language_edit",
            set(result["file_languages"].values())
            == {"typescript", "go"},
        )
        add(
            "non_authoritative",
            result["main_tree_modified"] is False
            and result["promotion_authorized"] is False,
        )

        try:
            editor.apply_text_edits(
                candidate,
                {"package.json": '{"dependencies":{"x":"1"}}'},
            )
            blocked_manifest = False
        except EngineeringCandidateEditError:
            blocked_manifest = True
        add("manifest_edit_blocked", blocked_manifest)

        try:
            editor.apply_text_edits(
                candidate,
                {"src/new.rs": "fn main() {}\n"},
            )
            blocked_language = False
        except EngineeringCandidateEditError:
            blocked_language = True
        add("foreign_language_blocked", blocked_language)

        try:
            editor.apply_text_edits(
                candidate,
                {"src/sira/runtime.py": "print('unsafe')\n"},
            )
            blocked_protected = False
        except EngineeringCandidateEditError:
            blocked_protected = True
        add("sira_protected_path_blocked", blocked_protected)

        source_text = (project / "src/app.ts").read_text(encoding="utf-8")
        add("main_tree_unchanged", source_text.endswith("= 1;\n"))

    report = {
        "schema_version": 1,
        "kind": "engineering_editing_benchmark",
        "suite_id": "sira-engineering-editing-v1.8d",
        "passed": sum(row["passed"] for row in cases),
        "failed": sum(not row["passed"] for row in cases),
        "api_requests": 0,
        "cases": cases,
    }
    store = RunStore(Path(root).resolve())
    path = store.path / "engineering-editing-benchmark.json"
    write_json(path, report)
    return path, report
