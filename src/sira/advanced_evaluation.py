"""A85: independent, versioned evaluation of isolated Python function changes.

Suites and expected values live outside candidate copyable paths. The trusted
runner supplies only a case input to the isolated subject. These reports are
advisory evidence; protected E1, authorization, and promotion remain separate.
"""
from __future__ import annotations

import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import random
import re
import resource
import shutil
import subprocess
import tempfile
import uuid

from .models import utc_now

MAX_CASES = 24
MAX_SUITE_BYTES = 32_768
MAX_RESULT_BYTES = 16_384
MAX_REPORT_BYTES = 96_000
MAX_INPUT_BYTES = 2_048
MAX_FRESH = 4
MAX_SUITES = 32
MAX_REPORTS = 64
TIMEOUT_SECONDS = 3
SCHEMA = "sira.advanced_evaluation.v1"
_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_TARGET = re.compile(r"src/(?:[a-z][a-z0-9_]*/)*[a-z][a-z0-9_]*\.py\Z")
_CLASSES = {"public", "hidden", "fresh", "out_of_distribution", "adversarial"}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _dir(root: Path) -> Path:
    return Path(root) / "memory" / "advanced_evaluation"


def _validate(suite: dict) -> None:
    if (not isinstance(suite, dict) or set(suite) != {
            "suite_id", "version", "target_path", "function", "cases"}
            or not isinstance(suite["suite_id"], str)
            or not _ID.fullmatch(suite["suite_id"])
            or not isinstance(suite["version"], str)
            or not _ID.fullmatch("v" + suite["version"])
            or not isinstance(suite["target_path"], str)
            or not _TARGET.fullmatch(suite["target_path"])
            or not isinstance(suite["function"], str)
            or not _ID.fullmatch(suite["function"])
            or not isinstance(suite["cases"], list)
            or not 2 <= len(suite["cases"]) <= MAX_CASES):
        raise ValueError("invalid bounded evaluation suite")
    ids = set()
    classes = set()
    for case in suite["cases"]:
        if (not isinstance(case, dict) or not {"id", "class", "input", "expected"} <= case.keys()
                or set(case) - {"id", "class", "input", "expected", "critical", "origin"}
                or not isinstance(case["id"], str) or not _ID.fullmatch(case["id"])
                or case["id"] in ids or not isinstance(case["class"], str)
                or case["class"] not in _CLASSES
                or type(case.get("critical", False)) is not bool
                or len(_json(case["input"])) > MAX_INPUT_BYTES
                or len(_json(case["expected"])) > MAX_INPUT_BYTES):
            raise ValueError("invalid case or duplicate case id")
        ids.add(case["id"])
        classes.add(case["class"])
    if not {"public", "hidden"} <= classes or len(_json(suite)) > MAX_SUITE_BYTES:
        raise ValueError("public and hidden cases required")


def register_suite(root: Path, suite: dict) -> Path:
    """Trusted owner registration only; never callable by candidate context."""
    _validate(suite)
    directory = _dir(root) / "suites"
    if directory.is_symlink():
        raise ValueError("unsafe suite directory")
    directory.mkdir(parents=True, exist_ok=True)
    if len(list(islice(directory.iterdir(), 2 * MAX_SUITES + 1))) >= 2 * MAX_SUITES:
        raise ValueError("suite registry over capacity")
    path = directory / (suite["suite_id"] + ".json")
    pin = directory / (suite["suite_id"] + ".sha256")
    if path.exists() or path.is_symlink() or pin.exists() or pin.is_symlink():
        raise ValueError("suite identity already registered")
    payload = _json(suite)
    # Exclusive writes; suite and pin are outside the copied candidate tree.
    for dest, data in ((path, payload), (pin, _sha(payload).encode("ascii"))):
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    return path


def load_suite(root: Path, suite_id: str) -> dict:
    if not isinstance(suite_id, str) or not _ID.fullmatch(suite_id):
        raise ValueError("invalid suite identity")
    directory = _dir(root) / "suites"
    path, pin = directory / (suite_id + ".json"), directory / (suite_id + ".sha256")
    if (directory.is_symlink() or not directory.is_dir()
            or any(p.is_symlink() or not p.is_file() for p in (path, pin))):
        raise ValueError("missing or unsafe suite material")
    if path.stat().st_size > MAX_SUITE_BYTES or pin.stat().st_size != 64:
        raise ValueError("invalid suite size")
    raw, expected = path.read_bytes(), pin.read_text("ascii")
    if _sha(raw) != expected:
        raise ValueError("suite integrity mismatch")
    suite = json.loads(raw)
    _validate(suite)
    if suite["suite_id"] != suite_id or _json(suite) != raw:
        raise ValueError("noncanonical suite")
    return suite


def candidate_visible_context(root: Path, suite_id: str) -> dict:
    suite = load_suite(root, suite_id)
    return {"suite_id": suite_id, "version": suite["version"],
            "target_path": suite["target_path"], "function": suite["function"],
            "public_cases": [{"id": c["id"], "input": c["input"]} for c in suite["cases"]
                             if c["class"] == "public"]}


def suites_for_target(root: Path, target: str) -> list[str]:
    """Bounded trusted registry lookup; malformed registered suites fail closed."""
    directory = _dir(root) / "suites"
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("unsafe suite registry")
    names = list(islice(directory.iterdir(), 2 * MAX_SUITES + 1))
    if len(names) > 2 * MAX_SUITES:
        raise ValueError("suite registry over capacity")
    suites = []
    for path in names:
        if path.suffix != ".json":
            continue
        suite = load_suite(root, path.stem)
        if suite["target_path"] == target:
            suites.append(path.stem)
    return sorted(suites)


def fresh_case_ids(suite: dict, seed: int) -> list[str]:
    if type(seed) is not int or not 0 <= seed < 2 ** 63:
        raise ValueError("invalid recorded seed")
    cases = sorted(c["id"] for c in suite["cases"] if c["class"] == "fresh")
    rng = random.Random(_sha(_json([suite["suite_id"], suite["version"], seed])))
    rng.shuffle(cases)
    return cases[:MAX_FRESH]


_SCRIPT = """import json,runpy,sys
p,fn,inp=sys.argv[1:]
if not p.startswith('/work/src/') or '..' in p: raise SystemExit(3)
f=runpy.run_path(p)[fn]
print(json.dumps(f(json.loads(inp)),allow_nan=False))
"""


def isolated_python_runner(subject: Path, target: str, function: str, value):
    """Fail closed if a private mount namespace is unavailable.

    The child sees only system Python and one source file; suite expectations,
    credentials, other repository paths and network are not mounted.
    """
    if not _TARGET.fullmatch(target) or not _ID.fullmatch(function):
        return {"status": "unavailable"}
    source = Path(subject) / target
    if (Path(subject).is_symlink() or any(p.is_symlink() for p in source.parents
            if p != Path(subject).parent)
            or source.is_symlink() or not source.is_file()
            or source.stat().st_size > 128_000):
        return {"status": "unavailable"}
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return {"status": "unavailable"}
    with tempfile.TemporaryDirectory(prefix="sira-a85-") as folder:
        work = Path(folder) / "src"
        work.mkdir()
        code = work / "subject.py"
        code.write_bytes(source.read_bytes())
        code.chmod(0o400)
        binds = []
        for system in ("/usr", "/lib", "/lib64", "/bin"):
            if Path(system).exists():
                binds.extend(["--ro-bind", system, system])
        argv = [bwrap, "--unshare-all", "--die-with-parent", "--new-session",
                "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--tmpfs", "/",
                *binds, "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--ro-bind", str(work.parent), "/work", "--chdir", "/work",
                "/usr/bin/python3", "-I", "-S", "-B", "-c", _SCRIPT,
                "/work/src/subject.py", function, _json(value).decode("utf-8")]
        def limits():
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESULT_BYTES, MAX_RESULT_BYTES))
            resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_SECONDS, TIMEOUT_SECONDS))
        with tempfile.TemporaryFile() as output:
            try:
                proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=output,
                                      stderr=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS,
                                      preexec_fn=limits, check=False, close_fds=True)
                output.seek(0)
                raw = output.read(MAX_RESULT_BYTES + 1)
                if proc.returncode or len(raw) > MAX_RESULT_BYTES:
                    return {"status": "incomplete"}
                return {"status": "completed", "observed": json.loads(raw)}
            except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
                return {"status": "unavailable"}


def evaluate_candidate(root: Path, baseline: Path, candidate: Path, suite_id: str,
                       *, seed: int, runner=None, baseline_id: str | None = None,
                       candidate_id: str | None = None) -> dict:
    suite = load_suite(root, suite_id)
    fresh = set(fresh_case_ids(suite, seed))
    cases = [c for c in suite["cases"] if c["class"] != "fresh" or c["id"] in fresh]
    if not cases or not Path(baseline).is_dir() or not Path(candidate).is_dir():
        raise ValueError("missing subjects or cases")
    runner = runner or isolated_python_runner
    outcomes = {}
    incomplete = False
    for name, subject in (("baseline", baseline), ("candidate", candidate)):
        rows = []
        for case in cases:
            try:
                result = runner(subject, suite["target_path"], suite["function"], case["input"])
            except Exception:
                result = {"status": "incomplete"}
            valid = isinstance(result, dict) and result.get("status") == "completed" and "observed" in result
            if not valid:
                incomplete = True
            rows.append({"case_id": case["id"], "class": case["class"],
                         "critical": case.get("critical", False), "status": "completed" if valid else "incomplete",
                         "passed": bool(valid and result["observed"] == case["expected"]),
                         "expected_result_sha256": _sha(_json(case["expected"])),
                         "observed_sha256": _sha(_json(result["observed"])) if valid else None})
        outcomes[name] = {"case_ids": [r["case_id"] for r in rows], "cases": rows,
                          **{cls: {"passed": all(r["passed"] for r in rows if r["class"] == cls),
                                    "count": sum(r["class"] == cls for r in rows)}
                             for cls in _CLASSES}}
    prior = outcomes["baseline"]["cases"]
    current = outcomes["candidate"]["cases"]
    regressions = [c["case_id"] for b, c in zip(prior, current)
                   if b["passed"] and not c["passed"]]
    verdict = "inconclusive" if incomplete else (
        "pass" if all(r["passed"] for r in current) and not regressions else "reject")
    identifier = "ae_" + uuid.uuid4().hex
    report = {"schema": SCHEMA, "evaluation_id": identifier, "created_at": utc_now(),
              "suite_id": suite_id, "suite_version": suite["version"], "suite_sha256": _sha(_json(suite)),
              "seed": seed, "case_provenance": [{"id": c["id"], "class": c["class"],
                 "origin": c.get("origin", "registered_local_suite"), "candidate_visible": c["class"] == "public",
                 "generator": "seeded_selection_v1" if c["class"] == "fresh" else None}
                 for c in cases],
              "baseline_id": baseline_id or "baseline_local", "candidate_id": candidate_id or "candidate_local",
              "results": outcomes, "regressions": regressions, "verdict": verdict,
              "evaluator": "independent_a85_v1", "candidate_self_certified": False,
              "promotion_authorized": False, "promotion_performed": False,
              "authority_granted": False, "paid_spending_authorized": False,
              "skill_activated": False, "api_requests": 0, "model_requests": 0}
    directory = _dir(root) / "reports"
    if directory.is_symlink():
        raise ValueError("unsafe evidence directory")
    directory.mkdir(parents=True, exist_ok=True)
    if len(list(islice(directory.iterdir(), 2 * MAX_REPORTS + 1))) >= 2 * MAX_REPORTS:
        raise ValueError("evaluation report capacity exceeded")
    path = directory / (identifier + ".json")
    payload = _json(report)
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError("evaluation report exceeds bounded artifact limit")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(payload)
        out.flush()
        os.fsync(out.fileno())
    pin = directory / (identifier + ".sha256")
    fd = os.open(pin, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(_sha(payload).encode("ascii"))
        out.flush()
        os.fsync(out.fileno())
    return report


def recent_independent_evidence(root: Path, *, limit: int = 4) -> list[dict]:
    """Bounded descriptive evidence for the self-model, never authorization."""
    directory = _dir(root) / "reports"
    if not directory.is_dir() or directory.is_symlink():
        return []
    entries = list(islice(directory.iterdir(), 2 * MAX_REPORTS + 1))
    if len(entries) > 2 * MAX_REPORTS:
        return []
    refs = []
    for path in sorted(entries, reverse=True):
        if not re.fullmatch(r"ae_[0-9a-f]{32}\.json", path.name):
            continue
        pin = path.with_suffix(".sha256")
        if any(p.is_symlink() or not p.is_file() for p in (path, pin)):
            continue
        if path.stat().st_size > MAX_REPORT_BYTES or pin.stat().st_size != 64:
            continue
        raw = path.read_bytes()
        if _sha(raw) != pin.read_text("ascii"):
            continue
        try:
            report = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if (report.get("schema") != SCHEMA or report.get("verdict") != "pass"
                or report.get("evaluator") != "independent_a85_v1"
                or report.get("evaluation_id") != path.stem
                or report.get("promotion_authorized") is not False):
            continue
        refs.append({"kind": "independent_evaluation", "ref": path.name,
                     "sha256": _sha(raw), "at": report.get("created_at"),
                     "scope": report.get("suite_id")})
        if len(refs) >= limit:
            break
    return refs
