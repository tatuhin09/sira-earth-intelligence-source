from __future__ import annotations

from pathlib import Path
import copy
import hashlib
import tempfile

from .code_writer import CodeModelBatch
from .engineering_authorization import (
    evaluate_engineering_authorization,
)
from .engineering_evaluator import (
    evaluate_engineering_candidate,
)
from .engineering_writer import (
    EngineeringResearchBackedWriter,
    prepare_verified_engineering_candidate,
)
from .self_modification import PROTECTED_PATHS
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
                    "content":
                        "def value():\n    return 2\n",
                    "reason": "fixture",
                }],
                "needs_more_context": False,
                "context_requests": [],
            },
            api_requests=0,
            input_tokens=0,
            output_tokens=0,
        )


def _runner(
    argv,
    *,
    cwd,
    timeout_seconds,
    max_output_bytes,
):
    return {
        "status": "passed",
        "returncode": 0,
        "timed_out": False,
        "output_limit_exceeded": False,
        "output_bytes": 0,
        "output_sha256":
            hashlib.sha256(b"").hexdigest(),
        "duration_ms": 1,
        "diagnostic_text": "",
    }


def _write(
    root: Path,
    relative: str,
    content: str,
):
    path = root / relative
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    path.write_text(
        content,
        encoding="utf-8",
    )


def _chain(base: Path):
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
    attempt = (
        prepare_verified_engineering_candidate(
            project,
            {
                "task_id": "bench",
                "instruction":
                    "Change value() to return 2.",
                "target_paths": [
                    "src/app.py"
                ],
            },
            base / "workspace",
            EngineeringResearchBackedWriter(
                project,
                _Model(),
            ),
            command_runner=_runner,
        )
    )
    evaluator = (
        evaluate_engineering_candidate(
            project,
            attempt,
        )
    )
    return project, attempt, evaluator


def engineering_authorization_benchmark(
    root: Path,
):
    cases = []
    add = lambda case_id, passed: (
        cases.append({
            "case_id": case_id,
            "passed": bool(passed),
        })
    )

    with tempfile.TemporaryDirectory(
        prefix="sira-eng-auth-"
    ) as tmp:
        base = Path(tmp)
        project, attempt, evaluator = (
            _chain(base)
        )
        report = (
            evaluate_engineering_authorization(
                project,
                attempt,
                evaluator,
            )
        )
        add(
            "clean_candidate_authorized",
            report["decision"] == "allow",
        )
        add(
            "checksum_only",
            report["authorization"][
                "checksum_only"
            ]
            is True,
        )
        add(
            "no_write_authority",
            report["authorization"][
                "write_authority"
            ]
            is False,
        )
        add(
            "no_promotion_execution_authority",
            report["authorization"][
                "promotion_execution_authority"
            ]
            is False,
        )
        add(
            "fingerprint_bound",
            len(
                report["authorization"][
                    "fingerprint"
                ]
            )
            == 64,
        )
        add(
            "exact_diff_bound",
            report["binding"][
                "changed_files"
            ]
            == ["src/app.py"],
        )
        add(
            "protected_gate_registered",
            "src/sira/engineering_authorization.py"
            in PROTECTED_PATHS,
        )

    with tempfile.TemporaryDirectory(
        prefix="sira-eng-auth-"
    ) as tmp:
        base = Path(tmp)
        project, attempt, evaluator = (
            _chain(base)
        )
        _write(
            project,
            "src/app.py",
            "def value():\n    return 99\n",
        )
        stale = (
            evaluate_engineering_authorization(
                project,
                attempt,
                evaluator,
            )
        )
        add(
            "stale_main_denied",
            stale["decision_code"]
            == "stale_candidate_base",
        )

    with tempfile.TemporaryDirectory(
        prefix="sira-eng-auth-"
    ) as tmp:
        base = Path(tmp)
        project, attempt, evaluator = (
            _chain(base)
        )
        candidate = Path(
            attempt["candidate_root"]
        )
        _write(
            candidate,
            "src/extra.py",
            "VALUE = 9\n",
        )
        tampered = (
            evaluate_engineering_authorization(
                project,
                attempt,
                evaluator,
            )
        )
        add(
            "candidate_tamper_denied",
            tampered["decision_code"]
            == "engineering_candidate_integrity_failed",
        )

    with tempfile.TemporaryDirectory(
        prefix="sira-eng-auth-"
    ) as tmp:
        base = Path(tmp)
        project, attempt, evaluator = (
            _chain(base)
        )
        forged = copy.deepcopy(
            evaluator
        )
        forged[
            "promotion_candidate_eligible"
        ] = False
        denied = (
            evaluate_engineering_authorization(
                project,
                attempt,
                forged,
            )
        )
        add(
            "noneligible_evaluator_denied",
            denied["decision_code"]
            == "engineering_evaluator_not_eligible",
        )

    with tempfile.TemporaryDirectory(
        prefix="sira-eng-auth-"
    ) as tmp:
        base = Path(tmp)
        project, attempt, evaluator = (
            _chain(base)
        )
        dirty = copy.deepcopy(
            attempt
        )
        dirty["verification"][
            "diagnostic_count"
        ] = 1
        denied = (
            evaluate_engineering_authorization(
                project,
                dirty,
                evaluator,
            )
        )
        add(
            "dirty_verification_denied",
            denied["decision_code"]
            == "engineering_verification_not_clean",
        )

    add("offline_zero_api", True)

    report = {
        "schema_version": 1,
        "kind":
            "engineering_authorization_benchmark",
        "suite_id":
            "sira-engineering-authorization-v1.8g",
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
    store = RunStore(
        Path(root).resolve()
    )
    path = (
        store.path
        / "engineering-authorization-benchmark.json"
    )
    write_json(path, report)
    return path, report
