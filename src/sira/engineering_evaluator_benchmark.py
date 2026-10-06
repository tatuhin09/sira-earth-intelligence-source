from __future__ import annotations

from pathlib import Path
import copy
import hashlib
import tempfile

from .code_writer import CodeModelBatch
from .engineering_evaluator import evaluate_engineering_candidate
from .engineering_writer import (
    EngineeringResearchBackedWriter,
    prepare_verified_engineering_candidate,
)
from .storage import RunStore, write_json


class _Model:
    name = "fixture_engineering_model"
    model_id = "fixture-v1"

    def generate_patch(self, payload, schema):
        return CodeModelBatch(
            {
                "summary": "change value",
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
    payload = b""
    return {
        "status": "passed",
        "returncode": 0,
        "timed_out": False,
        "output_limit_exceeded": False,
        "output_bytes": 0,
        "output_sha256": hashlib.sha256(payload).hexdigest(),
        "duration_ms": 1,
        "diagnostic_text": "",
    }


def _write(root: Path, relative: str, content: str):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _attempt(base: Path):
    project = base / "project"
    project.mkdir()
    _write(project, "src/app.py", "def value():\n    return 1\n")
    _write(
        project,
        "tests/test_app.py",
        "from src.app import value\n",
    )
    task = {
        "task_id": "bench",
        "instruction": "Change value() to return 2.",
        "target_paths": ["src/app.py"],
    }
    attempt = prepare_verified_engineering_candidate(
        project,
        task,
        base / "workspace",
        EngineeringResearchBackedWriter(project, _Model()),
        command_runner=_runner,
    )
    return project, attempt


def engineering_evaluator_benchmark(root: Path):
    cases = []
    add = lambda case_id, passed: cases.append(
        {"case_id": case_id, "passed": bool(passed)}
    )

    with tempfile.TemporaryDirectory(prefix="sira-eng-eval-") as tmp:
        base = Path(tmp)
        project, attempt = _attempt(base)
        report = evaluate_engineering_candidate(project, attempt)

        add("accepts_clean_verified_candidate",
            report["decision"] == "accept")
        add("candidate_is_eligible",
            report["promotion_candidate_eligible"] is True)
        add("never_authorizes_promotion",
            report["promotion_authorized"] is False)
        add("never_grants_authority",
            report["authority_granted"] is False)
        add("changed_set_bound",
            report["diff"]["actual_changed_files"] == ["src/app.py"])
        add("source_binding_current",
            report["source_binding"]["candidate_base_current"] is True)
        add("verification_clean",
            report["checks"]["verification_clean"] is True)
        add("diagnostic_advisory_clean",
            report["checks"]["diagnostic_advisory_clean"] is True)

        stale = copy.deepcopy(attempt)
        _write(project, "src/app.py", "def value():\n    return 7\n")
        stale_report = evaluate_engineering_candidate(project, stale)
        add("stale_main_blocks",
            stale_report["decision_code"] == "stale_candidate_base")

    with tempfile.TemporaryDirectory(prefix="sira-eng-eval-") as tmp:
        base = Path(tmp)
        project, attempt = _attempt(base)
        candidate = Path(attempt["candidate_root"])
        _write(candidate, "src/extra.py", "VALUE = 9\n")
        tampered = evaluate_engineering_candidate(project, attempt)
        add("undeclared_change_rejected",
            tampered["decision_code"] == "candidate_integrity_failed")

    with tempfile.TemporaryDirectory(prefix="sira-eng-eval-") as tmp:
        base = Path(tmp)
        project, attempt = _attempt(base)
        candidate = Path(attempt["candidate_root"])
        outside = base / "outside.py"
        outside.write_text("VALUE = 1\n", encoding="utf-8")
        (candidate / "src" / "linked.py").symlink_to(outside)
        linked = evaluate_engineering_candidate(project, attempt)
        add("symlink_rejected", "symlink_present" in linked["risk_flags"])

    with tempfile.TemporaryDirectory(prefix="sira-eng-eval-") as tmp:
        base = Path(tmp)
        project, attempt = _attempt(base)
        dirty = copy.deepcopy(attempt)
        dirty["verification"]["diagnostic_count"] = 1
        dirty["diagnostic_advisory"]["diagnostic_count"] = 1
        dirty_report = evaluate_engineering_candidate(project, dirty)
        add("diagnostic_failure_rejected",
            dirty_report["decision_code"] == "verification_not_clean")

    with tempfile.TemporaryDirectory(prefix="sira-eng-eval-") as tmp:
        base = Path(tmp)
        project, attempt = _attempt(base)
        forged = copy.deepcopy(attempt)
        forged["writer_report"]["promotion_authorized"] = True
        forged_report = evaluate_engineering_candidate(project, forged)
        add("forged_writer_authority_rejected",
            forged_report["decision_code"] == "attempt_integrity_failed")

    add("offline_zero_api", True)

    report = {
        "schema_version": 1,
        "kind": "engineering_evaluator_benchmark",
        "suite_id": "sira-engineering-evaluator-v1.8f",
        "passed": sum(row["passed"] for row in cases),
        "failed": sum(not row["passed"] for row in cases),
        "api_requests": 0,
        "cases": cases,
    }
    store = RunStore(Path(root).resolve())
    path = store.path / "engineering-evaluator-benchmark.json"
    write_json(path, report)
    return path, report
