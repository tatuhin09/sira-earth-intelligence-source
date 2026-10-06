from pathlib import Path
import hashlib
import tempfile

from .code_writer import CodeModelBatch
from .engineering_writer import (
    EngineeringResearchBackedWriter,
    build_engineering_writer_context,
    prepare_verified_engineering_candidate,
)
from .storage import RunStore, write_json


class _Model:
    name = "fixture_engineering_model"
    model_id = "fixture-v1"

    def __init__(self, data):
        self.data = data
        self.calls = 0

    def generate_patch(self, payload, schema):
        self.calls += 1
        return CodeModelBatch(
            self.data,
            api_requests=0,
            input_tokens=0,
            output_tokens=0,
        )


def _runner(status="passed", text=""):
    def run(
        argv,
        *,
        cwd,
        timeout_seconds,
        max_output_bytes,
    ):
        payload = text.encode()
        return {
            "status": status,
            "returncode": 0
            if status == "passed"
            else 1,
            "timed_out":
                status == "timeout",
            "output_limit_exceeded":
                status == "output_limit",
            "output_bytes": len(payload),
            "output_sha256":
                hashlib.sha256(payload).hexdigest(),
            "duration_ms": 1,
            "diagnostic_text": text,
        }
    return run


def _write(root: Path, relative: str, content: str):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def engineering_writer_benchmark(root: Path):
    cases = []
    add = lambda case_id, passed: cases.append(
        {"case_id": case_id, "passed": bool(passed)}
    )

    with tempfile.TemporaryDirectory(
        prefix="sira-eng-writer-"
    ) as tmp:
        base = Path(tmp)
        project = base / "project"
        project.mkdir()
        _write(
            project,
            "src/app.py",
            "def value():\n    return 1\n",
        )
        _write(
            project,
            "tests/test_app.py",
            "from src.app import value\n",
        )
        _write(
            project,
            ".env",
            "SECRET=hidden\n",
        )

        task = {
            "task_id": "fixture",
            "instruction":
                "Change value() to return 2.",
            "target_paths": ["src/app.py"],
        }
        context = build_engineering_writer_context(
            project,
            task,
        )
        add(
            "context_target_present",
            any(
                row["path"] == "src/app.py"
                for row in context["files"]
            ),
        )
        add(
            "secret_excluded",
            all(
                row["path"] != ".env"
                for row in context[
                    "available_context_index"
                ]
            ),
        )
        add(
            "context_candidate_only",
            context["editable_path_policy"][
                "candidate_only"
            ]
            is True,
        )

        model = _Model({
            "summary": "change value",
            "edits": [{
                "path": "src/app.py",
                "content":
                    "def value():\n    return 2\n",
                "reason": "requested behavior",
            }],
            "needs_more_context": False,
            "context_requests": [],
        })
        writer = EngineeringResearchBackedWriter(
            project,
            model,
        )
        result = prepare_verified_engineering_candidate(
            project,
            task,
            base / "workspace",
            writer,
            command_runner=_runner("passed"),
        )
        add(
            "candidate_verified",
            result["status"]
            == "verified_candidate_ready",
        )
        add(
            "main_unchanged",
            "return 1"
            in (
                project / "src/app.py"
            ).read_text(encoding="utf-8"),
        )
        add(
            "candidate_changed",
            "return 2"
            in (
                Path(result["candidate_root"])
                / "src/app.py"
            ).read_text(encoding="utf-8"),
        )
        add(
            "verification_executed",
            result["verification_executed"]
            is True,
        )
        add(
            "non_authoritative",
            result["promotion_authorized"]
            is False,
        )

        failing_model = _Model({
            "summary": "change value",
            "edits": [{
                "path": "src/app.py",
                "content":
                    "def value():\n    return bad\n",
                "reason": "fixture",
            }],
            "needs_more_context": False,
            "context_requests": [],
        })
        failing = prepare_verified_engineering_candidate(
            project,
            task,
            base / "workspace-fail",
            EngineeringResearchBackedWriter(
                project,
                failing_model,
            ),
            command_runner=_runner(
                "failed",
                "src/app.py:2:5: NameError: bad",
            ),
        )
        add(
            "failure_rejected",
            failing["status"]
            == "candidate_rejected",
        )
        add(
            "diagnostic_advisory_only",
            failing[
                "diagnostic_advisory"
            ]["repair_authority"]
            == "advisory_only",
        )
        add(
            "raw_output_absent",
            "diagnostic_text"
            not in repr(failing),
        )

        empty_model = _Model({
            "summary": "already correct",
            "edits": [],
            "needs_more_context": False,
            "context_requests": [],
        })
        empty = prepare_verified_engineering_candidate(
            project,
            task,
            base / "workspace-empty",
            EngineeringResearchBackedWriter(
                project,
                empty_model,
            ),
            command_runner=_runner("passed"),
        )
        add(
            "no_edit_nonmutating",
            empty["status"]
            == "no_edit_generated"
            and empty["candidate_root"] is None,
        )

        add(
            "model_call_bounded",
            model.calls == 1,
        )
        add(
            "zero_api_benchmark",
            True,
        )

    report = {
        "schema_version": 1,
        "kind": "engineering_writer_benchmark",
        "suite_id":
            "sira-engineering-writer-v1.8e",
        "passed": sum(
            row["passed"]
            for row in cases
        ),
        "failed": sum(
            not row["passed"]
            for row in cases
        ),
        "api_requests": 0,
        "cases": cases,
    }
    store = RunStore(Path(root).resolve())
    path = (
        store.path
        / "engineering-writer-benchmark.json"
    )
    write_json(path, report)
    return path, report
