"""Independent, local verification of an A74 provenance practice artifact.

This module never calls the practice executor and never writes a skill. A
completed run is only an input to a separately stored verification decision.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Iterator
from urllib.parse import urlsplit

from .learning_goal_skill_candidates import advisory_skill_candidates
from .learning_goals import LearningGoalStore
from .models import utc_now
from .storage import write_json


_CAPABILITY = "research.trace_verified_source_provenance.v1"
_GOAL_ID = re.compile(r"lg_[0-9a-f]{32}\Z")
_CANDIDATE_ID = re.compile(r"lsc_[0-9a-f]{24}\Z")
_RUN_ID = re.compile(r"lpr_[0-9a-f]{32}\Z")
_RECORD_KEYS = {
    "schema_version", "kind", "goal_id", "candidate_id", "expected_capability",
    "skill_activated", "skill_evidence_recorded", "promotion_performed",
    "authority_granted", "paid_spending", "run_id", "attempted_at_epoch",
    "created_at", "status", "failure_reason", "input", "execution_result",
    "artifacts", "resource_usage",
}
_INPUT_KEYS = {"candidate_id", "goal_id", "focus", "claim", "source_urls",
               "expected_capability"}
_RESULT_KEYS = {"candidate_id", "claim_sha256", "source_hosts",
                "source_url_sha256"}


class _VerificationFailure(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _reject_nonfinite(_value: str) -> None:
    raise ValueError("Non-finite JSON value")


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise _VerificationFailure("verification_destination_unsafe")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (not path.is_dir() or path.is_symlink()
            or path.stat().st_mode & 0o077):
        raise _VerificationFailure("verification_destination_unsafe")


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    _private_directory(directory)
    lock = directory / ".lock"
    if lock.is_symlink():
        raise _VerificationFailure("verification_destination_unsafe")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise _VerificationFailure("verification_destination_unsafe")
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _VerificationFailure("verification_lock_busy")
                time.sleep(.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path: Path, limit: int) -> tuple[bytes, object]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK |
                     getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        raise _VerificationFailure("practice_artifact_missing") from exc
    except OSError as exc:
        raise _VerificationFailure("practice_artifact_unsafe") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise _VerificationFailure("practice_artifact_unsafe")
        raw = os.read(fd, limit + 1)
        if len(raw) != info.st_size:
            raise _VerificationFailure("practice_artifact_changed")
        try:
            return raw, json.loads(raw, parse_constant=_reject_nonfinite)
        except (ValueError, UnicodeError) as exc:
            raise _VerificationFailure("verification_incomplete") from exc
    finally:
        os.close(fd)


def _current_candidate(root: Path, goal_id: str, candidate_id: str) -> dict | None:
    try:
        goal = LearningGoalStore(root).get(goal_id)
        if goal["status"] != "active" or goal["public_research_allowed"] is not True:
            return None
        return next((row for row in advisory_skill_candidates(root, goal, limit=10)
                     if row.get("candidate_id") == candidate_id), None)
    except (OSError, ValueError, RuntimeError, TypeError):
        return None


def _criteria(candidate: dict) -> tuple[dict, dict, int]:
    """Rebuild the A74 task from current verified evidence, never its output."""
    claim, focus, urls = candidate.get("claim"), candidate.get("focus"), candidate.get("source_urls")
    if (candidate.get("execution_mode") != "isolated_evidence_trace_v1"
            or candidate.get("executable") is not True
            or not isinstance(claim, str) or not 1 <= len(claim) <= 2_000
            or not isinstance(focus, str) or not 1 <= len(focus) <= 200
            or not isinstance(urls, list) or not 2 <= len(urls) <= 4
            or any(not isinstance(url, str) or not 1 <= len(url) <= 2_048 for url in urls)):
        raise _VerificationFailure("unsupported_practice_type")
    parts = [urlsplit(url) for url in urls]
    if (any(part.scheme != "https" or part.username or part.password
            or not part.hostname for part in parts)
            or len({part.hostname for part in parts}) < 2):
        raise _VerificationFailure("candidate_not_verified")
    expected_input = {"goal_id": candidate["goal_id"],
                      "candidate_id": candidate["candidate_id"], "focus": focus,
                      "claim": claim, "source_urls": urls,
                      "expected_capability": _CAPABILITY}
    expected_result = {
        "candidate_id": candidate["candidate_id"],
        "claim_sha256": _digest(claim.encode("utf-8")),
        "source_hosts": sorted({part.hostname for part in parts}),
        "source_url_sha256": [_digest(url.encode("utf-8")) for url in urls],
    }
    return expected_input, expected_result, len(expected_result["source_hosts"])


def verify_practice_run(root: Path, goal_id: str, candidate_id: str,
                        run_id: str) -> dict:
    """Write an independent decision; a passing exercise is only evidence."""
    if (not isinstance(goal_id, str) or not _GOAL_ID.fullmatch(goal_id)
            or not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(candidate_id)
            or not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id)):
        raise ValueError("Invalid practice verification identifiers")
    root = Path(root).resolve()
    memory = root / "memory"
    if memory.is_symlink():
        raise ValueError("Verification memory directory is unsafe")
    runs_root = memory / "learning_practice_runs"
    candidate_dir = runs_root / candidate_id
    run_dir = candidate_dir / run_id
    destination_root = memory / "learning_practice_verifications"
    if destination_root.is_symlink():
        raise ValueError("Verification destination is unsafe")
    destination = destination_root / candidate_id
    started = time.monotonic()
    with _locked(destination):
        output = destination / (run_id + ".json")
        source_paths = {name: run_dir / (name + ".json")
                        for name in ("record", "input", "result")}
        hashes: dict[str, str | None] = {name: None for name in source_paths}
        expected: dict | None = None
        observed: dict | None = None
        candidate: dict | None = None
        checks: dict[str, bool] = {}
        host_count = 0
        reason: str | None = None
        try:
            if any(path.is_symlink() for path in
                   (runs_root, candidate_dir, run_dir)):
                raise _VerificationFailure("practice_artifact_unsafe")
            record_raw, record = _read(source_paths["record"], 64_000)
            hashes["record"] = _digest(record_raw)
            if not isinstance(record, dict):
                raise _VerificationFailure("verification_incomplete")
            if set(record) != _RECORD_KEYS:
                reason = ("contaminated_practice_record" if set(record) - _RECORD_KEYS
                          else "verification_incomplete")
                raise _VerificationFailure(reason)
            checks["record_shape"] = True
            if (record.get("schema_version") != 1 or record.get("kind") != "learning_practice_run"
                    or record.get("goal_id") != goal_id
                    or record.get("candidate_id") != candidate_id
                    or record.get("run_id") != run_id
                    or record.get("expected_capability") != _CAPABILITY
                    or any(record.get(flag) is not False for flag in
                           ("skill_activated", "skill_evidence_recorded",
                            "promotion_performed", "authority_granted", "paid_spending"))):
                raise _VerificationFailure("contaminated_practice_record")
            attempted = record.get("attempted_at_epoch")
            created = record.get("created_at")
            if (type(attempted) not in (int, float) or not math.isfinite(attempted)
                    or not 0 <= attempted <= time.time() + 60
                    or not isinstance(created, str)):
                raise _VerificationFailure("verification_incomplete")
            try:
                created_time = datetime.fromisoformat(created)
            except ValueError as exc:
                raise _VerificationFailure("verification_incomplete") from exc
            if created_time.utcoffset() is None:
                raise _VerificationFailure("verification_incomplete")
            checks["run_provenance"] = True
            if record.get("status") != "completed" or record.get("failure_reason") is not None:
                raise _VerificationFailure("practice_not_completed")
            checks["executor_completed"] = True
            paths = record.get("artifacts")
            if (not isinstance(paths, dict) or set(paths) != set(source_paths)
                    or any(paths.get(name) != str(source_paths[name]) for name in source_paths)):
                raise _VerificationFailure("practice_artifact_mismatch")
            checks["artifact_identifiers"] = True
            input_raw, given_input = _read(source_paths["input"], 16_384)
            hashes["input"] = _digest(input_raw)
            result_raw, worker_result = _read(source_paths["result"], 4_096)
            hashes["result"] = _digest(result_raw)
            if not isinstance(given_input, dict) or set(given_input) != _INPUT_KEYS:
                raise _VerificationFailure("practice_input_mismatch")
            if isinstance(worker_result, dict):
                observed = {key: worker_result.get(key) for key in _RESULT_KEYS}
            else:
                raise _VerificationFailure("verification_incomplete")
            candidate = _current_candidate(root, goal_id, candidate_id)
            if candidate is None:
                raise _VerificationFailure("candidate_not_verified")
            expected_input, expected, host_count = _criteria(candidate)
            checks["fresh_candidate"] = True
            if given_input != expected_input or record.get("input") != expected_input:
                raise _VerificationFailure("practice_input_mismatch")
            checks["input_matches"] = True
            if (set(worker_result) != _RESULT_KEYS | {"cpu_seconds", "peak_rss_kib"}
                    or observed != expected):
                raise _VerificationFailure("practice_result_mismatch")
            checks["result_matches"] = True
            if record.get("execution_result") != expected:
                raise _VerificationFailure("practice_result_mismatch")
            usage = record.get("resource_usage")
            if (not isinstance(usage, dict)
                    or usage.get("api_requests") != 0
                    or usage.get("metered_model_requests") != 0
                    or usage.get("paid_requests") != 0
                    or usage.get("input_bytes") != len(input_raw)
                    or usage.get("output_bytes") != len(result_raw)
                    or type(worker_result.get("cpu_seconds")) not in (int, float)
                    or not 0 <= worker_result["cpu_seconds"] <= 3
                    or type(worker_result.get("peak_rss_kib")) is not int
                    or not 0 <= worker_result["peak_rss_kib"] <= 262_144
                    or usage.get("cpu_seconds") != worker_result["cpu_seconds"]
                    or usage.get("peak_rss_kib") != worker_result["peak_rss_kib"]):
                raise _VerificationFailure("practice_resource_mismatch")
            checks["resource_usage"] = True
            if (_current_candidate(root, goal_id, candidate_id) != candidate
                    or any(_digest(_read(path, limit)[0]) != hashes[name]
                           for name, path, limit in
                           (("record", source_paths["record"], 64_000),
                            ("input", source_paths["input"], 16_384),
                            ("result", source_paths["result"], 4_096)))):
                raise _VerificationFailure("practice_artifact_changed")
            checks["artifact_stable"] = True
        except _VerificationFailure as exc:
            reason = exc.reason
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            reason = "verification_incomplete"

        status = "verified" if reason is None else "rejected"
        candidate_key = candidate.get("knowledge_key") if candidate else None
        criteria_digest = _digest(_canonical(expected)) if expected is not None else None
        fingerprint = _digest(_canonical({
            "version": "independent_provenance_v1", "goal_id": goal_id,
            "candidate_id": candidate_id, "run_id": run_id, "status": status,
            "failure_reason": reason, "artifact_sha256": hashes,
            "criteria_sha256": criteria_digest,
        }))
        if output.is_symlink():
            raise ValueError("Verification artifact is unsafe")
        decision = {
            "schema_version": 1, "kind": "independent_practice_verification",
            "verifier_version": "independent_provenance_v1", "created_at": utc_now(),
            "goal_id": goal_id, "candidate_id": candidate_id, "run_id": run_id,
            "expected_capability": _CAPABILITY,
            "status": status, "practice_verified": status == "verified",
            "failure_reason": reason, "expected_result": expected,
            "observed_result": observed,
            "verifier_evidence": {"checks": checks, "independent_host_count": host_count,
                                  "knowledge_key": candidate_key},
            "provenance": {"practice_artifacts": {name: str(path)
                                                   for name, path in source_paths.items()},
                           "knowledge_key": candidate_key},
            "reproducibility": {"algorithm": "independent_provenance_v1",
                                "record_sha256": hashes["record"],
                                "input_sha256": hashes["input"],
                                "result_sha256": hashes["result"],
                                "criteria_sha256": criteria_digest,
                                "decision_fingerprint": fingerprint},
            "verifier_artifact": str(output),
            "resource_usage": {"api_requests": 0, "metered_model_requests": 0,
                               "paid_requests": 0,
                               "wall_seconds": round(time.monotonic() - started, 4)},
            "skill_activated": False, "mastery_certified": False,
            "lifecycle_evidence_only": True, "authority_granted": False,
        }
        write_json(output, decision)
        return decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify one completed A74 practice run")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("goal_id")
    parser.add_argument("candidate_id")
    parser.add_argument("run_id")
    args = parser.parse_args(argv)
    decision = verify_practice_run(args.root, args.goal_id,
                                   args.candidate_id, args.run_id)
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    return 0 if decision["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
