"""Bounded local provenance practice for rechecked learning candidates.

The only supported exercise traces a verified claim to independent source
hosts. It does not evaluate domain mastery or create consolidated skill evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Iterator
from urllib.parse import urlsplit
from uuid import uuid4

from .learning_goal_skill_candidates import advisory_skill_candidates
from .learning_goals import LearningGoalStore
from .models import utc_now
from .storage import write_json


CAPABILITY = "research.trace_verified_source_provenance.v1"
MAX_ATTEMPTS = 3
RETRY_SECONDS = 30 * 60
WALL_SECONDS = 3
MAX_INPUT_BYTES = 16_384
MAX_RESULT_BYTES = 4_096
_GOAL_ID = re.compile(r"lg_[0-9a-f]{32}\Z")
_CANDIDATE_ID = re.compile(r"lsc_[0-9a-f]{24}\Z")
_RUN_ID = re.compile(r"lpr_[0-9a-f]{32}\Z")

# Fixed code, not a candidate-supplied program. -I -S omits user packages;
# only one bounded input is read from the private working directory.
_WORKER = r'''
import os, resource
os.umask(0o077)
resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
import hashlib, json
from pathlib import Path
from urllib.parse import urlsplit
raw = Path("input.json").read_bytes()
if not 0 < len(raw) <= 16384:
    raise ValueError("bounded input required")
data = json.loads(raw)
urls = data["source_urls"]
hosts = sorted({urlsplit(url).hostname for url in urls})
result = {
    "candidate_id": data["candidate_id"],
    "claim_sha256": hashlib.sha256(data["claim"].encode("utf-8")).hexdigest(),
    "source_hosts": hosts,
    "source_url_sha256": [hashlib.sha256(url.encode("utf-8")).hexdigest() for url in urls],
}
usage = resource.getrusage(resource.RUSAGE_SELF)
result["cpu_seconds"] = round(usage.ru_utime + usage.ru_stime, 4)
result["peak_rss_kib"] = int(usage.ru_maxrss)
Path("result.json").write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
'''


def _safe_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Practice destination cannot be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise ValueError("Practice destination is unsafe")


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    _safe_directory(directory)
    lock = directory / ".lock"
    if lock.is_symlink():
        raise ValueError("Practice lock is unsafe")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("Practice lock is busy")
                time.sleep(.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _candidate(root: Path, goal_id: str, candidate_id: str) -> dict | None:
    try:
        goal = LearningGoalStore(root).get(goal_id)
        if goal["status"] != "active" or goal["public_research_allowed"] is not True:
            return None
        for candidate in advisory_skill_candidates(root, goal, limit=10):
            if candidate["candidate_id"] == candidate_id:
                return candidate
    except (OSError, ValueError, RuntimeError, TypeError):
        return None
    return None


def _input(candidate: dict) -> dict:
    focus, claim = candidate["focus"], candidate["claim"]
    urls = candidate["source_urls"]
    if (not isinstance(focus, str) or not 1 <= len(focus) <= 200
            or not isinstance(claim, str) or not 1 <= len(claim) <= 2_000
            or not isinstance(urls, list) or not 2 <= len(urls) <= 4
            or any(not isinstance(url, str) or not 1 <= len(url) <= 2_048
                   for url in urls)):
        raise ValueError("Practice candidate is not bounded")
    parsed = [urlsplit(url) for url in urls]
    if (any(url.scheme != "https" or url.username or url.password
            or not url.hostname for url in parsed)
            or len({url.hostname for url in parsed}) < 2):
        raise ValueError("Independent HTTPS sources required")
    return {"candidate_id": candidate["candidate_id"], "goal_id": candidate["goal_id"],
            "focus": focus, "claim": claim, "source_urls": urls,
            "expected_capability": CAPABILITY}


def _expected(data: dict) -> dict:
    return {"candidate_id": data["candidate_id"],
            "claim_sha256": hashlib.sha256(data["claim"].encode("utf-8")).hexdigest(),
            "source_hosts": sorted({urlsplit(url).hostname for url in data["source_urls"]}),
            "source_url_sha256": [hashlib.sha256(url.encode("utf-8")).hexdigest()
                                  for url in data["source_urls"]]}


def _previous_attempts(directory: Path, when: float) -> tuple[int, float | None]:
    if directory.is_symlink():
        raise ValueError("Practice candidate directory is unsafe")
    if not directory.exists():
        return 0, None
    if not directory.is_dir():
        raise ValueError("Practice candidate directory is unsafe")
    runs = list(directory.iterdir())
    if len(runs) > MAX_ATTEMPTS:
        raise ValueError("Practice attempt limit is corrupt")
    last = None
    for path in runs:
        if path.is_symlink() or not path.is_dir() or not _RUN_ID.fullmatch(path.name):
            raise ValueError("Practice attempt directory is unsafe")
        record = path / "record.json"
        if record.is_symlink() or not record.is_file() or record.stat().st_size > 64_000:
            raise ValueError("Practice attempt record is unsafe")
        saved = json.loads(record.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError("Practice attempt record is invalid")
        timestamp = saved.get("attempted_at_epoch")
        if (saved.get("schema_version") != 1 or saved.get("kind") != "learning_practice_run"
                or saved.get("run_id") != path.name
                or saved.get("candidate_id") != directory.name
                or saved.get("status") not in {"running", "completed", "failed", "rejected"}
                or type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                or timestamp < 0 or timestamp > when):
            raise ValueError("Practice attempt timestamp is invalid")
        last = max(timestamp, last or 0)
    return len(runs), last


def execute_practice_candidate(root: Path, goal_id: str, candidate_id: str, *,
                               now_epoch: float | None = None) -> dict:
    """Run one fixed local exercise; record its result without activating a skill."""
    if (not isinstance(goal_id, str) or not _GOAL_ID.fullmatch(goal_id)
            or not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(candidate_id)):
        raise ValueError("Invalid practice target")
    when = time.time() if now_epoch is None else now_epoch
    if type(when) not in (int, float) or not math.isfinite(when) or when < 0:
        raise ValueError("Invalid practice time")
    root = Path(root).resolve()
    memory = root / "memory"
    if memory.is_symlink():
        raise ValueError("Practice memory directory is unsafe")
    directory = memory / "learning_practice_runs"
    started = time.monotonic()
    with _locked(directory):
        candidate_dir = directory / candidate_id
        attempts, last = _previous_attempts(candidate_dir, float(when))
        base = {"schema_version": 1, "kind": "learning_practice_run",
                "goal_id": goal_id, "candidate_id": candidate_id,
                "expected_capability": CAPABILITY,
                "skill_activated": False, "skill_evidence_recorded": False,
                "promotion_performed": False, "authority_granted": False,
                "paid_spending": False}
        if attempts >= MAX_ATTEMPTS or (last is not None and when - last < RETRY_SECONDS):
            reason = "practice_attempt_limit" if attempts >= MAX_ATTEMPTS else "practice_cooldown"
            return {**base, "status": "deferred", "failure_reason": reason,
                    "execution_result": None, "artifacts": {},
                    "resource_usage": {"api_requests": 0, "metered_model_requests": 0,
                                       "paid_requests": 0}}
        _safe_directory(candidate_dir)
        run_id = "lpr_" + uuid4().hex
        run_dir = candidate_dir / run_id
        run_dir.mkdir(mode=0o700)
        input_path, output_path, record_path = (
            run_dir / "input.json", run_dir / "result.json", run_dir / "record.json")
        data = {"goal_id": goal_id, "candidate_id": candidate_id,
                "expected_capability": CAPABILITY}
        result = None
        status, failure = "rejected", "candidate_not_verified"
        usage = {"api_requests": 0, "metered_model_requests": 0, "paid_requests": 0,
                 "wall_seconds": 0.0, "cpu_seconds": 0.0, "peak_rss_kib": 0,
                 "input_bytes": 0, "output_bytes": 0}
        artifacts = {"input": str(input_path), "record": str(record_path)}
        # A crash leaves a durable, bounded interrupted attempt for recovery.
        write_json(record_path, {**base, "run_id": run_id,
                                 "attempted_at_epoch": float(when),
                                 "created_at": utc_now(), "status": "running",
                                 "failure_reason": "incomplete_interrupted",
                                 "input": data, "execution_result": None,
                                 "artifacts": artifacts, "resource_usage": usage})
        candidate = _candidate(root, goal_id, candidate_id)
        if candidate is not None:
            try:
                data = _input(candidate)
                payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if not 0 < len(payload) <= MAX_INPUT_BYTES:
                    raise ValueError("Practice input is too large")
                write_json(input_path, data)
                usage["input_bytes"] = input_path.stat().st_size
                status, failure = "failed", "practice_worker_failed"
                # An interrupted child has no trustworthy per-process CPU/RSS
                # report. Unknown is distinct from zero resource use.
                usage["cpu_seconds"] = None
                usage["peak_rss_kib"] = None
                completed = subprocess.run(
                    [sys.executable, "-I", "-S", "-c", _WORKER], cwd=run_dir,
                    env={"PYTHONIOENCODING": "utf-8"},
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=WALL_SECONDS, check=False,
                    start_new_session=True,
                )
                if completed.returncode != 0:
                    failure = "practice_worker_failed"
                elif (output_path.is_symlink() or not output_path.is_file()
                      or not 0 < output_path.stat().st_size <= MAX_RESULT_BYTES):
                    failure = "practice_result_missing_or_oversize"
                else:
                    raw = output_path.read_bytes()
                    usage["output_bytes"] = len(raw)
                    observed = json.loads(raw)
                    expected = _expected(data)
                    if (not isinstance(observed, dict)
                            or set(observed) != set(expected) | {"cpu_seconds", "peak_rss_kib"}
                            or any(observed[key] != value for key, value in expected.items())
                            or type(observed["cpu_seconds"]) not in (int, float)
                            or not 0 <= observed["cpu_seconds"] <= WALL_SECONDS
                            or type(observed["peak_rss_kib"]) is not int
                            or not 0 <= observed["peak_rss_kib"] <= 262_144):
                        failure = "practice_result_mismatch"
                    elif _candidate(root, goal_id, candidate_id) != candidate:
                        failure = "candidate_evidence_changed"
                    else:
                        result = expected
                        usage["cpu_seconds"] = observed["cpu_seconds"]
                        usage["peak_rss_kib"] = observed["peak_rss_kib"]
                        status, failure = "completed", None
                    artifacts["result"] = str(output_path)
            except subprocess.TimeoutExpired:
                status, failure = "failed", "practice_timeout"
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                status, failure = "failed", "practice_input_or_result_invalid"
        if not input_path.exists():
            write_json(input_path, data)
            usage["input_bytes"] = input_path.stat().st_size
        usage["wall_seconds"] = round(time.monotonic() - started, 4)
        report = {**base, "run_id": run_id, "attempted_at_epoch": float(when),
                  "created_at": utc_now(), "status": status, "failure_reason": failure,
                  "input": data, "execution_result": result,
                  "artifacts": artifacts, "resource_usage": usage}
        write_json(record_path, report)
        return report
