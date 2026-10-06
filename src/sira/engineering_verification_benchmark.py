from pathlib import Path
import hashlib
import tempfile

from .engineering_verification import execute_verification_plan, verification_execution_contract
from .storage import RunStore, write_json


def _plan(command_id="go.test", available=True):
    return {
        "schema": "sira.engineering_verification_plan.v1",
        "candidate_only_required": True,
        "network_disabled_required": True,
        "execution_performed": False,
        "commands": [{
            "command_id": command_id,
            "kind": "test",
            "language": "go",
            "argv": ["go", "test", "./..."],
            "cwd": ".",
            "env": {"GOPROXY": "off", "GOSUMDB": "off"},
            "available": available,
            "candidate_only": True,
            "network_policy": "sandbox_network_must_be_disabled",
            "writes_generated_artifacts": True,
            "shell": False,
            "authority_granted": False,
            "promotion_authorized": False,
        }],
    }


def _runner(status="passed"):
    def run(argv, *, cwd, timeout_seconds, max_output_bytes):
        payload = (" ".join(argv)).encode()
        return {
            "status": status,
            "returncode": 0 if status == "passed" else 1,
            "timed_out": status == "timeout",
            "output_limit_exceeded": status == "output_limit",
            "output_bytes": len(payload),
            "output_sha256": hashlib.sha256(payload).hexdigest(),
            "duration_ms": 1,
        }
    return run


def engineering_verification_benchmark(root: Path):
    root = Path(root).resolve()
    cases = []
    add = lambda case_id, passed: cases.append({"case_id": case_id, "passed": bool(passed)})
    with tempfile.TemporaryDirectory(prefix="sira-eng-verify-") as tmp:
        candidate = Path(tmp) / "candidate"
        candidate.mkdir()

        contract = verification_execution_contract(_plan())
        add("contract_ready", contract["status"] == "ready")
        add("contract_non_authoritative", contract["promotion_authorized"] is False)

        good = execute_verification_plan(candidate, _plan(), command_runner=_runner("passed"))
        add("pass_executes", good["overall_passed"] is True and good["processes_executed"] == 1)
        add("no_raw_output", good["raw_output_included"] is False)
        add("credential_clear", good["credentials_inherited"] is False)
        add("no_promotion", good["promotion_authorized"] is False)

        bad = execute_verification_plan(candidate, _plan(), command_runner=_runner("failed"))
        add("failure_stops", bad["outcome"] == "verification_failed")

        timed = execute_verification_plan(candidate, _plan(), command_runner=_runner("timeout"))
        add("timeout_fails", timed["outcome"] == "verification_timeout")

        capped = execute_verification_plan(candidate, _plan(), command_runner=_runner("output_limit"))
        add("output_cap_fails", capped["outcome"] == "verification_output_limit")

        missing = execute_verification_plan(candidate, _plan(available=False), command_runner=_runner())
        add("missing_tool_blocks", missing["outcome"] == "required_tool_unavailable" and missing["processes_executed"] == 0)

        no_sandbox = execute_verification_plan(candidate, _plan(), bubblewrap_path="", command_runner=None)
        add("no_isolation_blocks", no_sandbox["outcome"] == "network_filesystem_isolation_unavailable")

        add(
            "main_tree_unchanged_contract",
            good["main_tree_modified"] is False and good["package_installation_performed"] is False,
        )

    report = {
        "schema_version": 1,
        "kind": "engineering_verification_benchmark",
        "suite_id": "sira-engineering-verification-v1.8b",
        "passed": sum(row["passed"] for row in cases),
        "failed": sum(not row["passed"] for row in cases),
        "api_requests": 0,
        "cases": cases,
    }
    store = RunStore(root)
    path = store.path / "engineering-verification-benchmark.json"
    write_json(path, report)
    return path, report
