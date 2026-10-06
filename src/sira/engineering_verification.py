"""Isolated, fail-closed execution of v1.8A engineering verification plans.

This module never installs dependencies, never uses a shell, never executes in
SIRA's main tree, and never grants promotion authority. Default execution
requires Bubblewrap network/filesystem isolation. If the isolation backend is
unavailable, verification is blocked rather than silently weakened.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence

from .engineering_diagnostics import parse_diagnostics

ENGINEERING_VERIFICATION_POLICY_VERSION = 1
MAX_COMMANDS = 8
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 180
MAX_TOTAL_SECONDS = 300
MAX_OUTPUT_BYTES = 256 * 1024
MAX_ARGC = 64
MAX_ARG_BYTES = 4096

_ALLOWED_COMMANDS = frozenset({
    "python.pytest", "python.unittest",
    "node.lint", "node.test", "node.build",
    "typescript.typecheck",
    "flutter.analyze", "flutter.test",
    "dart.analyze", "dart.test",
    "java.maven_test", "java.gradle_test",
    "cmake.configure", "cmake.build", "cmake.test",
    "rust.check", "rust.test",
    "go.test", "sql.sqlfluff",
})
_ALLOWED_KINDS = frozenset({"lint", "test", "typecheck", "configure", "build"})
_FORBIDDEN_EXECUTABLES = frozenset({
    "sudo", "su", "doas", "curl", "wget", "ssh", "scp", "sftp",
    "apt", "apt-get", "dnf", "yum", "pacman", "apk", "brew",
    "pip", "pip3", "npx",
})


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _validate_candidate_root(candidate_root: Path, main_root: Path | None) -> Path:
    raw = Path(candidate_root)
    if raw.is_symlink():
        raise ValueError("candidate root must not be a symlink")
    candidate = raw.resolve()
    if not candidate.is_dir():
        raise ValueError("candidate root must exist")
    if main_root is not None:
        main = Path(main_root).resolve()
        if candidate == main:
            raise ValueError("engineering verification cannot execute in the main tree")
        try:
            candidate.relative_to(main)
        except ValueError:
            pass
        else:
            raise ValueError("candidate workspace must not be nested inside the main tree")
    return candidate


def _validate_argv(command_id: str, argv: object) -> list[str]:
    if command_id not in _ALLOWED_COMMANDS:
        raise ValueError("unsupported engineering verification command")
    if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGC:
        raise ValueError("invalid verification argv")
    result: list[str] = []
    for value in argv:
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or len(value.encode("utf-8")) > MAX_ARG_BYTES
        ):
            raise ValueError("invalid verification argument")
        result.append(value)

    executable = Path(result[0]).name.casefold()
    if executable in _FORBIDDEN_EXECUTABLES:
        raise ValueError("forbidden verification executable")

    if command_id == "python.pytest":
        if len(result) < 4 or result[1:3] != ["-m", "pytest"]:
            raise ValueError("invalid pytest adapter argv")
    elif command_id == "python.unittest":
        if result[1:4] != ["-m", "unittest", "discover"]:
            raise ValueError("invalid unittest adapter argv")
    elif command_id.startswith("node."):
        if executable not in {"npm", "pnpm", "yarn", "bun"}:
            raise ValueError("invalid Node package-manager adapter")
        if any(token in {"install", "add", "update", "upgrade", "ci"} for token in result[1:]):
            raise ValueError("dependency mutation is forbidden")
    elif command_id == "typescript.typecheck":
        if not result[0].startswith("./node_modules/.bin/tsc") or "--noEmit" not in result[1:]:
            raise ValueError("invalid TypeScript adapter argv")
    elif command_id.startswith("flutter."):
        if executable != "flutter":
            raise ValueError("invalid Flutter adapter argv")
    elif command_id.startswith("dart."):
        if executable != "dart":
            raise ValueError("invalid Dart adapter argv")
    elif command_id == "java.maven_test":
        if executable not in {"mvn", "mvnw"} or "-o" not in result:
            raise ValueError("Maven verification must be offline")
    elif command_id == "java.gradle_test":
        if executable not in {"gradle", "gradlew"} or "--offline" not in result:
            raise ValueError("Gradle verification must be offline")
    elif command_id.startswith("cmake."):
        if command_id == "cmake.test":
            if executable != "ctest":
                raise ValueError("invalid CTest adapter argv")
        elif executable != "cmake":
            raise ValueError("invalid CMake adapter argv")
        if command_id == "cmake.configure" and not any(
            token == "-DFETCHCONTENT_FULLY_DISCONNECTED=ON" for token in result
        ):
            raise ValueError("CMake configure must disable FetchContent networking")
    elif command_id.startswith("rust."):
        if executable != "cargo" or "--offline" not in result:
            raise ValueError("Cargo verification must be offline")
    elif command_id == "go.test":
        if executable != "go" or "test" not in result[1:]:
            raise ValueError("invalid Go test adapter argv")
    elif command_id == "sql.sqlfluff":
        if executable != "sqlfluff" or "lint" not in result[1:]:
            raise ValueError("invalid SQLFluff adapter argv")
    return result


def _validated_commands(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(plan, Mapping):
        raise ValueError("verification plan must be a mapping")
    if plan.get("schema") != "sira.engineering_verification_plan.v1":
        raise ValueError("unsupported engineering verification plan")
    if plan.get("candidate_only_required") is not True:
        raise ValueError("verification plan must be candidate-only")
    if plan.get("network_disabled_required") is not True:
        raise ValueError("verification plan must require network isolation")
    if plan.get("execution_performed") is not False:
        raise ValueError("verification plan must be unexecuted input")
    commands = plan.get("commands")
    if not isinstance(commands, list) or not 1 <= len(commands) <= MAX_COMMANDS:
        raise ValueError("verification plan must contain 1..8 commands")

    validated: list[dict[str, Any]] = []
    for row in commands:
        if not isinstance(row, Mapping):
            raise ValueError("invalid verification command row")
        command_id = _text(row.get("command_id"))
        kind = _text(row.get("kind"))
        language = _text(row.get("language"))
        if command_id is None or kind not in _ALLOWED_KINDS or language is None:
            raise ValueError("invalid verification command metadata")
        if row.get("candidate_only") is not True or row.get("shell") is not False:
            raise ValueError("verification command violates candidate/shell policy")
        if row.get("network_policy") != "sandbox_network_must_be_disabled":
            raise ValueError("verification command lacks network isolation policy")
        if row.get("authority_granted") is not False or row.get("promotion_authorized") is not False:
            raise ValueError("verification command contains forbidden authority")
        if row.get("cwd") != ".":
            raise ValueError("verification command cwd must remain candidate root")
        env = row.get("env") or {}
        if not isinstance(env, Mapping):
            raise ValueError("verification command env must be a mapping")
        safe_env: dict[str, str] = {}
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("verification command env must be text")
            if key not in {"GOPROXY", "GOSUMDB"}:
                raise ValueError("verification plan may not inject arbitrary environment")
            safe_env[key] = value
        if command_id == "go.test":
            if safe_env.get("GOPROXY") != "off" or safe_env.get("GOSUMDB") != "off":
                raise ValueError("Go verification must disable module network access")
        validated.append({
            "command_id": command_id,
            "kind": kind,
            "language": language,
            "argv": _validate_argv(command_id, row.get("argv")),
            "available": row.get("available") is True,
            "env": safe_env,
            "writes_generated_artifacts": row.get("writes_generated_artifacts") is True,
        })
    return validated


def verification_execution_contract(plan: Mapping[str, Any]) -> dict[str, Any]:
    try:
        commands = _validated_commands(plan)
        status = "ready" if all(row["available"] for row in commands) else "missing_tool"
        available = sum(row["available"] for row in commands)
        count = len(commands)
    except (ValueError, TypeError):
        status, available, count = "invalid_plan", 0, 0
    return {
        "schema": "sira.engineering_verification_contract.v1",
        "policy_version": ENGINEERING_VERIFICATION_POLICY_VERSION,
        "status": status,
        "command_count": count,
        "available_command_count": available,
        "default_isolation_backend": "bubblewrap",
        "network_namespace_required": True,
        "filesystem_main_tree_read_only": True,
        "credential_environment_inherited": False,
        "shell_execution_allowed": False,
        "package_installation_allowed": False,
        "execution_performed": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }


def _safe_environment(candidate: Path, extra: Mapping[str, str]) -> dict[str, str]:
    home = candidate / ".sira-verify-home"
    cache = candidate / ".sira-verify-cache"
    tmp = candidate / ".sira-verify-tmp"
    for path in (home, cache, tmp):
        path.mkdir(mode=0o700, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CACHE_HOME": str(cache),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "1",
        "NO_COLOR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "npm_config_offline": "true",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "YARN_ENABLE_NETWORK": "0",
        "PUB_ENVIRONMENT": "sira:offline",
        "GRADLE_OPTS": "-Dorg.gradle.daemon=false",
        "MAVEN_OPTS": "-Dstyle.color=never",
        "CARGO_NET_OFFLINE": "true",
    }
    env.update(extra)
    return env


def _bubblewrap_prefix(candidate: Path, bwrap: str) -> list[str]:
    return [
        bwrap,
        "--die-with-parent",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-pid",
        "--cap-drop", "ALL",
        "--ro-bind", "/", "/",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", str(candidate), str(candidate),
        "--chdir", str(candidate),
        "--clearenv",
    ]


def _wrapped_command(candidate: Path, argv: Sequence[str], env: Mapping[str, str], bwrap: str) -> list[str]:
    wrapped = _bubblewrap_prefix(candidate, bwrap)
    for key, value in sorted(env.items()):
        wrapped.extend(["--setenv", key, value])
    wrapped.append("--")
    wrapped.extend(argv)
    return wrapped


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int,
) -> dict[str, Any]:
    started = time.monotonic()
    digest = hashlib.sha256()
    capture = bytearray()
    output_bytes = 0
    timed_out = False
    output_limit_exceeded = False
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env={},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return {
            "status": "spawn_error",
            "returncode": None,
            "timed_out": False,
            "output_limit_exceeded": False,
            "output_bytes": 0,
            "output_sha256": hashlib.sha256(b"").hexdigest(),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "_diagnostic_text": "",
        }

    selector = selectors.DefaultSelector()
    if proc.stdout is not None:
        selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = started + timeout_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process_group(proc)
                break
            events = selector.select(timeout=min(0.1, remaining))
            for key, _ in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 8192)
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(key.fileobj)
                    except Exception:
                        pass
                    continue
                output_bytes += len(chunk)
                digest.update(chunk)
                remaining_capture = max_output_bytes - len(capture)
                if remaining_capture > 0:
                    capture.extend(chunk[:remaining_capture])
                if output_bytes > max_output_bytes:
                    output_limit_exceeded = True
                    _kill_process_group(proc)
                    break
            if output_limit_exceeded:
                break
            if proc.poll() is not None and not selector.get_map():
                break
        try:
            returncode = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            returncode = proc.wait(timeout=2)
    finally:
        selector.close()
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass

    status = (
        "timeout" if timed_out
        else "output_limit" if output_limit_exceeded
        else "passed" if returncode == 0
        else "failed"
    )
    return {
        "status": status,
        "returncode": returncode,
        "timed_out": timed_out,
        "output_limit_exceeded": output_limit_exceeded,
        "output_bytes": output_bytes,
        "output_sha256": digest.hexdigest(),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "_diagnostic_text": capture.decode("utf-8", errors="replace"),
    }


CommandRunner = Callable[..., Mapping[str, Any]]


def execute_verification_plan(
    candidate_root: Path,
    plan: Mapping[str, Any],
    *,
    main_root: Path | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    command_runner: CommandRunner | None = None,
    bubblewrap_path: str | None = None,
) -> dict[str, Any]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError("engineering verification timeout must be 1..180 seconds")
    if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= MAX_OUTPUT_BYTES:
        raise ValueError("engineering verification output cap must be 1KiB..256KiB")

    candidate = _validate_candidate_root(candidate_root, main_root)
    commands = _validated_commands(plan)
    contract = verification_execution_contract(plan)

    missing = [row["command_id"] for row in commands if not row["available"]]
    if missing:
        return {
            "schema": "sira.engineering_verification_result.v1",
            "policy_version": ENGINEERING_VERIFICATION_POLICY_VERSION,
            "status": "blocked",
            "outcome": "required_tool_unavailable",
            "overall_passed": False,
            "isolation_backend": None,
            "commands": [],
            "missing_command_ids": missing,
            "contract": contract,
            "processes_executed": 0,
            "shell_used": False,
            "network_isolation_enforced": False,
            "credentials_inherited": False,
            "package_installation_performed": False,
            "main_tree_modified": False,
            "authority_granted": False,
            "promotion_authorized": False,
        }

    runner = command_runner or _run_bounded
    bwrap = bubblewrap_path
    if command_runner is None:
        bwrap = (
            shutil.which("bwrap")
            if bubblewrap_path is None
            else bubblewrap_path
        )
        if not bwrap:
            return {
                "schema": "sira.engineering_verification_result.v1",
                "policy_version": ENGINEERING_VERIFICATION_POLICY_VERSION,
                "status": "blocked",
                "outcome": "network_filesystem_isolation_unavailable",
                "overall_passed": False,
                "isolation_backend": None,
                "commands": [],
                "missing_command_ids": [],
                "contract": contract,
                "processes_executed": 0,
                "shell_used": False,
                "network_isolation_enforced": False,
                "credentials_inherited": False,
                "package_installation_performed": False,
                "main_tree_modified": False,
                "authority_granted": False,
                "promotion_authorized": False,
            }

    started_total = time.monotonic()
    rows: list[dict[str, Any]] = []
    executed = 0
    isolation_backend = "test_injected" if command_runner is not None else "bubblewrap"

    for command in commands:
        remaining_total = MAX_TOTAL_SECONDS - (time.monotonic() - started_total)
        if remaining_total <= 0:
            rows.append({
                "command_id": command["command_id"],
                "kind": command["kind"],
                "language": command["language"],
                "status": "total_timeout",
                "returncode": None,
                "timed_out": True,
                "output_limit_exceeded": False,
                "output_bytes": 0,
                "output_sha256": hashlib.sha256(b"").hexdigest(),
                "duration_ms": 0,
            })
            break

        env = _safe_environment(candidate, command["env"])
        argv = (
            list(command["argv"])
            if command_runner is not None
            else _wrapped_command(candidate, command["argv"], env, str(bwrap))
        )
        result = runner(
            argv,
            cwd=candidate,
            timeout_seconds=min(timeout_seconds, max(1, int(remaining_total))),
            max_output_bytes=max_output_bytes,
        )
        executed += 1
        if not isinstance(result, Mapping):
            raise ValueError("engineering command runner returned invalid result")
        status = _text(result.get("status")) or "runner_error"
        diagnostic_text = result.get("_diagnostic_text")
        if command_runner is not None and not isinstance(diagnostic_text, str):
            diagnostic_text = result.get("diagnostic_text")
        diagnostic_report = parse_diagnostics(
            command["command_id"], command["language"],
            diagnostic_text if isinstance(diagnostic_text, str) else "",
            root=candidate,
        )
        row = {
            "command_id": command["command_id"],
            "kind": command["kind"],
            "language": command["language"],
            "status": status,
            "returncode": result.get("returncode") if type(result.get("returncode")) is int else None,
            "timed_out": result.get("timed_out") is True,
            "output_limit_exceeded": result.get("output_limit_exceeded") is True,
            "output_bytes": result.get("output_bytes") if type(result.get("output_bytes")) is int else 0,
            "output_sha256": _text(result.get("output_sha256")),
            "duration_ms": result.get("duration_ms") if type(result.get("duration_ms")) is int else None,
            "diagnostic_count": diagnostic_report["diagnostic_count"],
            "diagnostic_summary": diagnostic_report["severity_counts"],
            "diagnostics": diagnostic_report["diagnostics"],
        }
        rows.append(row)
        if status != "passed":
            break

    passed = len(rows) == len(commands) and all(row["status"] == "passed" for row in rows)
    if passed:
        outcome, final_status = "verification_passed", "completed"
    elif any(row["status"] in {"timeout", "total_timeout"} for row in rows):
        outcome, final_status = "verification_timeout", "failed"
    elif any(row["status"] == "output_limit" for row in rows):
        outcome, final_status = "verification_output_limit", "failed"
    else:
        outcome, final_status = "verification_failed", "failed"

    diagnostics = [
        diagnostic for command_row in rows
        for diagnostic in command_row.get("diagnostics", [])
        if isinstance(diagnostic, dict)
    ]
    return {
        "schema": "sira.engineering_verification_result.v1",
        "policy_version": ENGINEERING_VERIFICATION_POLICY_VERSION,
        "status": final_status,
        "outcome": outcome,
        "overall_passed": passed,
        "isolation_backend": isolation_backend,
        "commands": rows,
        "diagnostics": diagnostics,
        "diagnostic_count": len(diagnostics),
        "missing_command_ids": [],
        "contract": contract,
        "processes_executed": executed,
        "shell_used": False,
        "network_isolation_enforced": True,
        "credentials_inherited": False,
        "package_installation_performed": False,
        "main_tree_modified": False,
        "authority_granted": False,
        "promotion_authorized": False,
        "raw_output_included": False,
    }
