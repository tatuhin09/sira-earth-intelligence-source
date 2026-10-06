"""Deterministic failure -> research -> hypothesis -> experiment foundation.

This module deliberately does not edit SIRA's main source tree. It can select a
memory, persist a bounded hypothesis, prepare an isolated candidate workspace,
and run only fixed offline regression commands. Promotion is a later milestone.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Protocol
from uuid import uuid4

from . import __version__
from .memory import MemoryStore
from .models import utc_now
from .storage import write_json
from .self_modification import prepare_candidate_workspace
from .evaluator2 import evaluate_candidate
from .engineering_diagnostics import parse_diagnostics

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
PLAN_ID_RE = re.compile(r"ip_[0-9a-f]{32}\Z")
HYPOTHESIS_ID_RE = re.compile(r"ih_[0-9a-f]{32}\Z")
EXPERIMENT_ID_RE = re.compile(r"ix_[0-9a-f]{32}\Z")
RESEARCH_CONTEXT_POLICY_VERSION = 2

CAPABILITY_SUITES = {
    "retrieval": "retrieval",
    "reading": "reading",
    "synthesis": "synthesis",
    "scholarly_research": "papers",
    "paper_reading": "paper-reading",
    "memory": "memory",
}

CATEGORY_GUIDANCE = {
    "rate_limit": (
        "Provider throttling may be mitigated by bounded retry, cache, and independent fallback providers.",
        "Verify the current fallback/cache path against the paper benchmark before proposing more retry pressure.",
    ),
    "network": (
        "Transient transport failures should be isolated, bounded, and recoverable through cache or provider fallback.",
        "Verify current failure isolation and avoid unbounded retries.",
    ),
    "provider": (
        "A single provider failure should not collapse a capability when an independent fallback exists.",
        "Verify provider failure isolation with the capability regression suite.",
    ),
    "citation": (
        "Citation failures are best addressed by provenance validation and fail-closed reference resolution.",
        "Verify citation gates before changing synthesis behavior.",
    ),
    "verification": (
        "Verification failures require stricter evidence binding rather than more permissive acceptance.",
        "Verify the current synthesis rejection gates and provenance checks.",
    ),
    "bad_source": (
        "Unsafe or unreadable sources should be rejected per-source while useful sources continue.",
        "Verify source isolation and fallback behavior before adding parser complexity.",
    ),
    "parsing": (
        "Parser failures should fail closed and preserve the surrounding run when possible.",
        "Verify malformed-input regression coverage before adding another parser path.",
    ),
    "model": (
        "Model-output failures should be contained by bounded structured output and deterministic local validation.",
        "Verify model output gates instead of trusting model self-reports.",
    ),
    "benchmark_regression": (
        "A benchmark regression should be reproduced with the smallest deterministic fixture before any change.",
        "Run the full relevant regression suite in isolation.",
    ),
    "code_test": (
        "Code/test failures require a reproducible regression test and an isolated candidate workspace.",
        "Verify the current tree and candidate separately before any promotion.",
    ),
    "unknown": (
        "The failure is not classified strongly enough to justify a broad code change.",
        "Collect deterministic diagnostics and run the closest capability benchmark first.",
    ),
}


def _project_digest(root: Path) -> str:
    root = Path(root).resolve()
    digest = hashlib.sha256()
    candidates: list[Path] = []
    for relative in ("sira.py", "src", "tests", "benchmarks"):
        path = root / relative
        if path.is_file():
            candidates.append(path)
        elif path.is_dir():
            for item in path.rglob("*"):
                if item.is_file() and not item.is_symlink() and "__pycache__" not in item.parts:
                    candidates.append(item)
    for path in sorted(candidates, key=lambda p: str(p.relative_to(root))):
        rel = str(path.relative_to(root)).encode("utf-8")
        digest.update(rel + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _hypothesis_fingerprint(memory: dict, statement: str, suite: str | None) -> str:
    payload = {
        "memory_id": memory["memory_id"],
        "category": memory["category"],
        "capability": memory["capability"],
        "statement": " ".join(statement.casefold().split()),
        "suite": suite,
        "research_context_policy_version": RESEARCH_CONTEXT_POLICY_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ImprovementStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.base = self.root / "improvements"
        self.plans = self.base / "plans"
        self.hypotheses = self.base / "hypotheses"
        self.experiments = self.base / "experiments"
        self.workspaces = self.base / "workspaces"
        for path in (self.plans, self.hypotheses, self.experiments, self.workspaces):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def plan_path(self, plan_id: str) -> Path:
        if not PLAN_ID_RE.fullmatch(plan_id):
            raise ValueError("Invalid improvement plan ID")
        return self.plans / f"{plan_id}.json"

    def hypothesis_path(self, hypothesis_id: str) -> Path:
        if not HYPOTHESIS_ID_RE.fullmatch(hypothesis_id):
            raise ValueError("Invalid hypothesis ID")
        return self.hypotheses / f"{hypothesis_id}.json"

    def experiment_path(self, experiment_id: str) -> Path:
        if not EXPERIMENT_ID_RE.fullmatch(experiment_id):
            raise ValueError("Invalid experiment ID")
        return self.experiments / f"{experiment_id}.json"

    @staticmethod
    def _load(path: Path, *, kind: str, id_key: str, expected_id: str) -> dict:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("Improvement artifact not found or unsafe")
        try:
            data = json.loads(path.read_bytes())
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Invalid improvement artifact JSON") from None
        if (not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("kind") != kind
                or data.get(id_key) != expected_id):
            raise ValueError("Invalid improvement artifact format")
        return data

    def load_hypothesis(self, hypothesis_id: str) -> dict:
        path = self.hypothesis_path(hypothesis_id)
        return self._load(path, kind="improvement_hypothesis", id_key="hypothesis_id", expected_id=hypothesis_id)

    def save_plan(self, value: dict) -> None:
        write_json(self.plan_path(value["plan_id"]), value)

    def save_hypothesis(self, value: dict) -> None:
        write_json(self.hypothesis_path(value["hypothesis_id"]), value)

    def save_experiment(self, value: dict) -> None:
        write_json(self.experiment_path(value["experiment_id"]), value)

    def find_hypothesis_by_fingerprint(self, fingerprint: str) -> dict | None:
        files = sorted(self.hypotheses.glob("ih_*.json"))
        if len(files) > 2000:
            raise ValueError("Too many improvement hypotheses")
        for path in files:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError, OSError):
                continue
            if data.get("kind") == "improvement_hypothesis" and data.get("fingerprint") == fingerprint:
                return data
        return None

    def latest_experiment_for_hypothesis(self, hypothesis_id: str) -> dict | None:
        if not HYPOTHESIS_ID_RE.fullmatch(hypothesis_id):
            raise ValueError("Invalid hypothesis ID")
        matches = []
        for path in self.experiments.glob("ix_*.json"):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError, OSError):
                continue
            if (data.get("schema_version") == 1 and data.get("kind") == "improvement_experiment"
                    and data.get("hypothesis_id") == hypothesis_id):
                matches.append((data.get("created_at") or "", data))
        if not matches:
            return None
        matches.sort(key=lambda item: item[0], reverse=True)
        return matches[0][1]

    def status(self) -> dict:
        def count(pattern: str, directory: Path) -> int:
            return sum(1 for p in directory.glob(pattern) if p.is_file() and not p.is_symlink())
        recent = []
        safe_experiments = [p for p in self.experiments.glob("ix_*.json")
                            if p.is_file() and not p.is_symlink()]
        for path in sorted(safe_experiments, key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                recent.append({"experiment_id": item.get("experiment_id"), "outcome": item.get("outcome"),
                               "memory_id": item.get("memory_id")})
            except (ValueError, OSError):
                continue
        return {"status": "ok", "plans": count("ip_*.json", self.plans),
                "hypotheses": count("ih_*.json", self.hypotheses),
                "experiments": count("ix_*.json", self.experiments), "recent_experiments": recent}


def plan_improvement(root: Path) -> dict:
    root = Path(root).resolve()
    memory = MemoryStore(root)
    candidates = memory.improvement_candidates(5)
    if not candidates:
        raise ValueError("No observed or rejected failure memory is available for improvement")
    target, alternatives = candidates[0], candidates[1:]
    plan_id = "ip_" + uuid4().hex
    plan = {
        "schema_version": 1,
        "sira_version": __version__,
        "kind": "improvement_plan",
        "plan_id": plan_id,
        "created_at": utc_now(),
        "code_sha256": _project_digest(root),
        "target": target,
        "alternatives": alternatives,
        "selection_policy": ("failure/rejection only; observed or rejected; synthetic fixture/mock/dummy "
                             "memories excluded by default; genuine benchmark regressions remain eligible; "
                             "weighted by frequency, category, and kind"),
        "next_action": f"python sira.py improve research {target['memory_id']}",
    }
    ImprovementStore(root).save_plan(plan)
    return plan


def _research_statement(memory: dict) -> tuple[str, str]:
    category = memory.get("category") if memory.get("category") in CATEGORY_GUIDANCE else "unknown"
    rationale, action = CATEGORY_GUIDANCE[category]
    statement = f"For {memory['capability']}, {rationale} {action}"
    return statement, action


def research_memory(root: Path, memory_id: str) -> dict:
    root = Path(root).resolve()
    memory_store = MemoryStore(root)
    memory = memory_store.show(memory_id)
    if memory["kind"] not in ("failure", "rejection"):
        raise ValueError("Only failure or rejection memories can start an improvement hypothesis")
    if memory["status"] in ("validated", "superseded"):
        raise ValueError("This memory is already resolved")

    related = [
        row for row in memory_store.retrieve(
            memory["capability"], 20, autonomous=True
        )
        if row["memory_id"] != memory_id
    ][:5]
    for row in related:
        memory_store.record_outcome(
            row["memory_id"],
            "retrieval_used",
            context={
                "consumer": "improvement.research_memory",
                "query": memory["capability"],
                "target_memory_id": memory_id,
            },
            weight=1.0,
        )
    validated_successes = [
        row for row in related
        if row["kind"] == "success" and row["status"] == "validated"
    ]
    statement, recommended_action = _research_statement(memory)
    suite = CAPABILITY_SUITES.get(memory["capability"])
    fingerprint = _hypothesis_fingerprint(memory, statement, suite)
    store = ImprovementStore(root)
    existing = store.find_hypothesis_by_fingerprint(fingerprint)
    if existing is not None:
        latest = store.latest_experiment_for_hypothesis(existing["hypothesis_id"])
        if (memory["status"] == "rejected" and latest is not None
                and str(latest.get("outcome", "")).startswith("rejected")):
            result = dict(existing)
            result["reused"] = True
            result["repeat_blocked"] = True
            return result
        if memory["status"] in ("observed", "rejected"):
            memory_store.transition(memory_id, "researched", "Structured root-cause research started")
        result = dict(existing)
        result["reused"] = True
        result["repeat_blocked"] = False
        return result

    if memory["status"] in ("observed", "rejected"):
        memory_store.transition(memory_id, "researched", "Structured root-cause research started")
        memory = memory_store.show(memory_id)

    hypothesis_id = "ih_" + uuid4().hex
    hypothesis = {
        "schema_version": 1,
        "sira_version": __version__,
        "kind": "improvement_hypothesis",
        "hypothesis_id": hypothesis_id,
        "fingerprint": fingerprint,
        "created_at": utc_now(),
        "memory_id": memory_id,
        "memory_snapshot": {key: memory.get(key) for key in
                            ("kind", "category", "capability", "provider", "error_code", "summary",
                             "occurrence_count", "status")},
        "statement": statement,
        "rationale": recommended_action,
        "related_memories": [{key: row.get(key) for key in
                              ("memory_id", "kind", "status", "category", "capability", "provider", "error_code",
                               "summary", "occurrence_count", "retrieval_score", "score_reasons")}
                             for row in related],
        "validated_successes_found": len(validated_successes),
        "research_context_policy_version": RESEARCH_CONTEXT_POLICY_VERSION,
        "research_context_policy": ("synthetic fixture/mock/dummy memories excluded from autonomous research "
                                    "context; genuine benchmark regressions remain eligible"),
        "experiment_type": "regression_check",
        "benchmark_suite": suite,
        "success_criteria": {
            "unit_tests_must_pass": True,
            "relevant_benchmark_must_pass": bool(suite),
            "no_main_tree_modification": True,
        },
        "reused": False,
        "repeat_blocked": False,
    }
    store.save_hypothesis(hypothesis)
    return hypothesis


class ExperimentRunner(Protocol):
    def evaluate(self, root: Path, suite: str | None) -> dict: ...


@dataclass
class SubprocessExperimentRunner:
    timeout_seconds: int = 120

    @staticmethod
    def _bounded_output(value: str) -> str:
        value = value or ""
        return value[-6000:]

    def _run(self, root: Path, args: list[str]) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        for name in ("TAVILY_API_KEY", "GEMINI_API_KEY", "SIRA_SEMANTIC_SCHOLAR_API_KEY"):
            env.pop(name, None)
        return subprocess.run(args, cwd=root, env=env, capture_output=True, text=True,
                              timeout=self.timeout_seconds, check=False)

    def evaluate(self, root: Path, suite: str | None) -> dict:
        root = Path(root).resolve()
        try:
            tests = self._run(root, [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"])
        except subprocess.TimeoutExpired:
            return {"tests": {"passed": False, "test_count": None, "returncode": None, "timed_out": True},
                    "benchmark": None, "overall_passed": False}
        test_output = (tests.stdout or "") + "\n" + (tests.stderr or "")
        match = re.search(r"Ran\s+(\d+)\s+tests?", test_output)
        test_diagnostics = parse_diagnostics(
            "python.unittest", "python", test_output, root=root
        )
        test_result = {
            "passed": tests.returncode == 0,
            "test_count": int(match.group(1)) if match else None,
            "returncode": tests.returncode,
            "diagnostic_count": test_diagnostics["diagnostic_count"],
            "diagnostics": test_diagnostics["diagnostics"],
            "raw_output_included": False,
        }
        benchmark_result = None
        if suite:
            flags = {
                "retrieval": [], "reading": ["--reading"], "synthesis": ["--synthesis"],
                "papers": ["--papers"], "paper-reading": ["--paper-reading"], "memory": ["--memory"],
            }
            if suite not in flags:
                raise ValueError("Unsupported experiment benchmark suite")
            try:
                bench = self._run(root, [sys.executable, "sira.py", "benchmark", *flags[suite]])
                parsed = None
                for line in reversed((bench.stdout or "").splitlines()):
                    try:
                        candidate = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(candidate, dict) and "passed" in candidate and "failed" in candidate:
                        parsed = candidate
                        break
                benchmark_result = {
                    "passed": bench.returncode == 0 and bool(parsed) and parsed.get("failed") == 0,
                    "passed_cases": parsed.get("passed") if parsed else None,
                    "failed_cases": parsed.get("failed") if parsed else None,
                    "returncode": bench.returncode,
                    "raw_output_included": False,
                }
            except subprocess.TimeoutExpired:
                benchmark_result = {"passed": False, "passed_cases": None, "failed_cases": None,
                                    "returncode": None, "timed_out": True}
        return {"tests": test_result, "benchmark": benchmark_result,
                "overall_passed": test_result["passed"] and (benchmark_result is None or benchmark_result["passed"])}



def run_experiment(root: Path, hypothesis_id: str, *, runner: ExperimentRunner | None = None) -> dict:
    root = Path(root).resolve()
    store = ImprovementStore(root)
    hypothesis = store.load_hypothesis(hypothesis_id)
    latest = store.latest_experiment_for_hypothesis(hypothesis_id)
    if latest is not None and str(latest.get("outcome", "")).startswith("rejected"):
        raise ValueError("Rejected hypothesis cannot be repeated without a new strategy")
    memory_id = hypothesis.get("memory_id")
    memory = MemoryStore(root)
    detail = memory.show(memory_id)
    if detail["status"] == "researched":
        memory.transition(memory_id, "experimenting", f"Experiment started for {hypothesis_id}")
    elif detail["status"] not in ("experimenting",):
        raise ValueError("Memory is not ready for experimentation")

    experiment_id = "ix_" + uuid4().hex
    workspace = store.workspaces / experiment_id
    candidate_root = workspace / "candidate"
    candidate_policy = prepare_candidate_workspace(root, candidate_root)
    runner = runner or SubprocessExperimentRunner()
    suite = hypothesis.get("benchmark_suite")
    baseline = runner.evaluate(root, suite)
    candidate = runner.evaluate(candidate_root, suite)
    evaluator2 = evaluate_candidate(root, candidate_root, baseline, candidate)

    if not baseline.get("overall_passed"):
        outcome = "blocked_unhealthy_baseline"
        memory.transition(memory_id, "researched", "Experiment blocked because the current baseline is unhealthy")
    elif not candidate.get("overall_passed"):
        outcome = "rejected_regression"
        memory.transition(memory_id, "rejected", "Candidate failed unit tests or the relevant benchmark")
    elif evaluator2.get("decision") != "accept":
        outcome = "rejected_evaluator2"
        memory.transition(memory_id, "rejected", "Evaluator 2 rejected the candidate structural diff")
    elif hypothesis.get("experiment_type") == "regression_check":
        outcome = "validated_existing_mitigation"
        memory.transition(memory_id, "validated", "Current isolated regression checks pass for the historical failure")
    else:
        outcome = "prepared_no_change"

    result = {
        "schema_version": 1,
        "sira_version": __version__,
        "kind": "improvement_experiment",
        "experiment_id": experiment_id,
        "hypothesis_id": hypothesis_id,
        "memory_id": memory_id,
        "created_at": utc_now(),
        "experiment_type": hypothesis.get("experiment_type"),
        "benchmark_suite": suite,
        "workspace": str(workspace),
        "main_tree_sha256": _project_digest(root),
        "candidate_tree_sha256": _project_digest(candidate_root),
        "candidate_policy": candidate_policy,
        "baseline": baseline,
        "candidate": candidate,
        "evaluator2": evaluator2,
        "outcome": outcome,
        "main_tree_modified": False,
        "promotion_performed": False,
    }
    store.save_experiment(result)
    return result
