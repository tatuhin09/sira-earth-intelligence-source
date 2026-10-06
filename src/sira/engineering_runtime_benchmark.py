from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile

from .code_writer import CodeModelBatch
from .engineering_runtime import (
    engineering_runtime_eligible,
    run_engineering_runtime_handoff,
)
from .engineering_writer import EngineeringResearchBackedWriter
from .storage import RunStore, write_json


class _Model:
    name = "fixture_engineering_model"
    model_id = "fixture-v1"

    def generate_patch(self, payload, schema):
        return CodeModelBatch(
            {
                "summary": "bounded runtime fixture",
                "edits": [{
                    "path": "src/app.py",
                    "content": "def value():\n    return 2\n",
                    "reason": "fixture",
                }],
                "needs_more_context": False,
                "context_requests": [],
            },
            api_requests=0,
            input_tokens=0,
            output_tokens=0,
        )


def _runner(argv, *, cwd, timeout_seconds, max_output_bytes):
    return {
        "status": "passed",
        "returncode": 0,
        "timed_out": False,
        "output_limit_exceeded": False,
        "output_bytes": 0,
        "output_sha256": hashlib.sha256(b"").hexdigest(),
        "duration_ms": 1,
        "diagnostic_text": "",
    }


def _write(root: Path, relative: str, content: str):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _inputs(root: Path):
    target = {
        "target_kind": "opportunity",
        "opportunity_id": "op_" + "a" * 32,
        "path": "src/app.py",
    }
    evidence = {
        "opportunity": {
            "opportunity_id": target["opportunity_id"],
            "fingerprint": "b" * 64,
            "type": "explicit_debt_marker",
            "path": "src/app.py",
            "symbol": "value",
            "summary": "simplify the fixture while preserving behavior",
        },
        "assessment": {"decision": "research_ready"},
        "success_criteria": {
            "preserve_current_observable_behavior": True,
            "unit_tests_must_pass": True,
        },
    }
    research = {
        "research_id": "or_" + "c" * 32,
        "writer_handoff_allowed": True,
        "research_quality": {"decision": "writer_ready"},
        "paid_spending": False,
        "metered_model_requests": 0,
        "candidate_strategies": ["make the smallest tested source change"],
        "citations": [],
    }
    return target, evidence, research


def engineering_runtime_benchmark(root: Path):
    cases = []

    def add(case_id, passed):
        cases.append({"case_id": case_id, "passed": bool(passed)})

    with tempfile.TemporaryDirectory(prefix="sira-eng-runtime-") as tmp:
        base = Path(tmp)
        project = base / "project"
        project.mkdir()
        _write(project, "src/app.py", "def value():\n    return 1\n")
        _write(project, "tests/test_app.py", "from src.app import value\n")
        target, evidence, research = _inputs(project)

        add(
            "eligible_supported_target",
            engineering_runtime_eligible(project, target, evidence, research),
        )

        result = run_engineering_runtime_handoff(
            project,
            target,
            evidence,
            research,
            worker_task_id="mw_fixture",
            runtime_guard=lambda: True,
            writer_factory=lambda r: EngineeringResearchBackedWriter(r, _Model()),
            command_runner=_runner,
            state_root=base / "external",
        )
        add("protected_route_selected", result["runtime_route"] == "protected_engineering_v1")
        add("promotion_committed", result["outcome"] == "promotion_committed")
        add("promotion_performed", result["promotion_performed"] is True)
        add("main_modified_only_after_promotion", result["main_tree_modified"] is True)
        add("writer_bound", isinstance(result["writer_report"], dict))
        add("evaluator_bound", isinstance(result["engineering_evaluator"], dict))
        add("authorization_bound", isinstance(result["engineering_authorization"], dict))
        add("guard_checked", result["runtime_guard_checked_before_promotion"] is True)
        add("no_package_install", result["package_installation_performed"] is False)
        add("no_main_command_execution", result["main_tree_command_execution"] is False)
        add(
            "source_promoted",
            "return 2" in (project / "src/app.py").read_text(encoding="utf-8"),
        )

    report = {
        "schema_version": 1,
        "kind": "engineering_runtime_benchmark",
        "suite_id": "sira-engineering-runtime-v1.8i",
        "passed": sum(row["passed"] for row in cases),
        "failed": sum(not row["passed"] for row in cases),
        "api_requests": 0,
        "cases": cases,
    }
    store = RunStore(Path(root).resolve())
    path = store.path / "engineering-runtime-benchmark.json"
    write_json(path, report)
    return path, report
