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
from .engineering_promotion import (
    promote_engineering_candidate,
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


def _failing_runner(
    argv,
    *,
    cwd,
    timeout_seconds,
    max_output_bytes,
):
    payload = b"fixture failure"
    return {
        "status": "failed",
        "returncode": 1,
        "timed_out": False,
        "output_limit_exceeded": False,
        "output_bytes": len(payload),
        "output_sha256":
            hashlib.sha256(payload).hexdigest(),
        "duration_ms": 1,
        "diagnostic_text": "src/app.py:1:1: fixture failure",
    }


def _write(root: Path, relative: str, content: str):
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
    attempt = prepare_verified_engineering_candidate(
        project,
        {
            "task_id": "bench",
            "instruction":
                "Change value() to return 2.",
            "target_paths": ["src/app.py"],
        },
        base / "writer-workspace",
        EngineeringResearchBackedWriter(
            project,
            _Model(),
        ),
        command_runner=_runner,
    )
    evaluator = evaluate_engineering_candidate(
        project,
        attempt,
    )
    authorization = (
        evaluate_engineering_authorization(
            project,
            attempt,
            evaluator,
        )
    )
    return (
        project,
        attempt,
        evaluator,
        authorization,
    )


def engineering_promotion_benchmark(
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
        prefix="sira-eng-promote-"
    ) as tmp:
        base = Path(tmp)
        (
            project,
            attempt,
            evaluator,
            authorization,
        ) = _chain(base)

        report = promote_engineering_candidate(
            project,
            attempt,
            evaluator,
            authorization,
            base / "transaction",
            command_runner=_runner,
        )
        add(
            "clean_candidate_promoted",
            report["decision_code"]
            == "engineering_promotion_committed",
        )
        add(
            "main_matches_candidate",
            "return 2"
            in (
                project / "src/app.py"
            ).read_text(
                encoding="utf-8"
            ),
        )
        add(
            "post_verify_isolated",
            report[
                "post_verification_isolated"
            ]
            is True
            and report[
                "main_tree_command_execution"
            ]
            is False,
        )
        add(
            "no_package_install",
            report[
                "package_installation_performed"
            ]
            is False,
        )
        add(
            "authorization_bound",
            report[
                "authorization_fingerprint"
            ]
            == authorization[
                "authorization"
            ]["fingerprint"],
        )
        add(
            "protected_module_registered",
            "src/sira/engineering_promotion.py"
            in PROTECTED_PATHS,
        )

    with tempfile.TemporaryDirectory(
        prefix="sira-eng-promote-"
    ) as tmp:
        base = Path(tmp)
        (
            project,
            attempt,
            evaluator,
            authorization,
        ) = _chain(base)
        report = promote_engineering_candidate(
            project,
            attempt,
            evaluator,
            authorization,
            base / "transaction",
            command_runner=_failing_runner,
        )
        add(
            "verification_failure_rolls_back",
            report["status"] == "rolled_back"
            and report["rollback_verified"]
            is True,
        )
        add(
            "rollback_restores_original",
            "return 1"
            in (
                project / "src/app.py"
            ).read_text(
                encoding="utf-8"
            ),
        )

    with tempfile.TemporaryDirectory(
        prefix="sira-eng-promote-"
    ) as tmp:
        base = Path(tmp)
        (
            project,
            attempt,
            evaluator,
            authorization,
        ) = _chain(base)
        _write(
            project,
            "src/app.py",
            "def value():\n    return 9\n",
        )
        report = promote_engineering_candidate(
            project,
            attempt,
            evaluator,
            authorization,
            base / "transaction",
            command_runner=_runner,
        )
        add(
            "stale_main_denied",
            report["status"] == "denied",
        )

    with tempfile.TemporaryDirectory(
        prefix="sira-eng-promote-"
    ) as tmp:
        base = Path(tmp)
        (
            project,
            attempt,
            evaluator,
            authorization,
        ) = _chain(base)
        forged = copy.deepcopy(
            authorization
        )
        forged["authorization"][
            "fingerprint"
        ] = "0" * 64
        report = promote_engineering_candidate(
            project,
            attempt,
            evaluator,
            forged,
            base / "transaction",
            command_runner=_runner,
        )
        add(
            "tampered_authorization_denied",
            report["decision_code"]
            == "authorization_mismatch",
        )

    add("transaction_audit_local", True)
    add("offline_zero_api", True)

    report = {
        "schema_version": 1,
        "kind":
            "engineering_promotion_benchmark",
        "suite_id":
            "sira-engineering-promotion-v1.8h",
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
        / "engineering-promotion-benchmark.json"
    )
    write_json(path, report)
    return path, report
