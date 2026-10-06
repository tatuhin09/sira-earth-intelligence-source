"""Auto Benchmark / Evaluator v1.5B intelligence layer.

This module is modifiable and non-authoritative. It selects only already
supported offline benchmark suites, normalizes measured health, compares
verification strength, and turns repeated *current-code* benchmark failures
into bounded prioritization evidence.

No result from this module can authorize promotion, change protected policy,
grant external access, or create a new benchmark capability.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

AUTO_EVALUATOR_POLICY_VERSION = 2
MAX_HISTORY_REPORTS = 200
MAX_HISTORY_BYTES = 2 * 1024 * 1024
RECURRENCE_THRESHOLD = 2
MAX_BENCHMARK_PRIORITY_BOOST = 160

SUPPORTED_BENCHMARKS: dict[str, tuple[str, ...]] = {
    "retrieval": (),
    "reading": ("--reading",),
    "papers": ("--papers",),
    "paper-reading": ("--paper-reading",),
    "synthesis": ("--synthesis",),
    "memory": ("--memory",),
    "improvement": ("--improvement",),
}

_CAPABILITY_TO_SUITE = {
    "retrieval": "retrieval",
    "web_research": "retrieval",
    "reading": "reading",
    "source_reading": "reading",
    "scholarly_research": "papers",
    "paper_search": "papers",
    "papers": "papers",
    "paper_reading": "paper-reading",
    "pdf_reading": "paper-reading",
    "verified_synthesis": "synthesis",
    "synthesis": "synthesis",
    "learning_memory": "memory",
    "memory": "memory",
    "benchmarking": "improvement",
    "self_improvement": "improvement",
    "autonomous_improvement": "improvement",
}

_PATH_RULES: tuple[tuple[str, str], ...] = (
    ("src/sira/retrieval", "retrieval"),
    ("src/sira/paper_reading", "paper-reading"),
    ("src/sira/providers/pdf_text", "paper-reading"),
    ("src/sira/synthesis", "synthesis"),
    ("src/sira/memory", "memory"),
    ("src/sira/papers", "papers"),
    ("src/sira/providers/paper_", "papers"),
    ("src/sira/providers/openalex", "papers"),
    ("src/sira/providers/arxiv", "papers"),
    ("src/sira/providers/crossref", "papers"),
    ("src/sira/reading", "reading"),
    ("src/sira/improvement", "improvement"),
    ("src/sira/autonomous_", "improvement"),
    ("src/sira/evolution", "improvement"),
    ("src/sira/evaluator2", "improvement"),
    ("src/sira/opportunity_", "improvement"),
    ("src/sira/runtime", "improvement"),
)


def project_code_digest(root: Path) -> str:
    """Digest current project src/sira Python using the runtime benchmark shape."""
    source_root = Path(root).resolve() / "src" / "sira"
    digest = hashlib.sha256()
    if not source_root.is_dir():
        return digest.hexdigest()
    for path in sorted(source_root.rglob("*.py")):
        if path.is_symlink() or not path.is_file():
            continue
        digest.update(
            str(path.relative_to(source_root)).encode("utf-8")
            + b"\0"
            + path.read_bytes()
            + b"\0"
        )
    return digest.hexdigest()


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _nested_text(value: Mapping[str, object], *keys: str) -> str | None:
    current: object = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return _text(current)


def _suite_from_path(path: str | None) -> str | None:
    if path is None:
        return None
    normalized = path.replace("\\", "/").casefold()
    for prefix, suite in _PATH_RULES:
        if normalized.startswith(prefix):
            return suite
    return None


def _suite_from_capability(capability: str | None) -> str | None:
    if capability is None:
        return None
    return _CAPABILITY_TO_SUITE.get(capability.casefold())


def suite_from_report(value: Mapping[str, object]) -> str | None:
    """Map bounded benchmark metadata to one supported suite."""
    explicit = _text(value.get("suite"))
    if explicit in SUPPORTED_BENCHMARKS:
        return explicit
    suite_id = (_text(value.get("suite_id")) or "").casefold()
    kind = (_text(value.get("kind")) or "").casefold()
    material = f"{suite_id} {kind}"
    if "paper-reading" in material or "paper_reading" in material:
        return "paper-reading"
    if "paper" in material:
        return "papers"
    if "synthesis" in material:
        return "synthesis"
    if "memory" in material:
        return "memory"
    if "reading" in material:
        return "reading"
    if (
        "improvement" in material
        or "evolution" in material
        or "strategy" in material
        or "auto-evaluator" in material
        or "auto_evaluator" in material
    ):
        return "improvement"
    if "retrieval" in material or "fixture" in material:
        return "retrieval"
    return None


def benchmark_history_summary(root: Path, suite: str) -> dict[str, object]:
    """Return bounded recurrence metadata for one supported benchmark suite.

    Only reports explicitly bound to the current project source digest can
    establish recurrence. A one-off failure is evidence but does not receive a
    recurrence boost. Stale/unknown reports are audit-only.
    """
    if suite not in SUPPORTED_BENCHMARKS:
        raise ValueError("unsupported benchmark suite")
    root = Path(root).resolve()
    current_sha = project_code_digest(root)
    reports = 0
    current_reports = 0
    current_failures = 0
    current_passes = 0
    stale_or_unknown = 0
    latest_current: dict[str, object] | None = None

    runs = root / "runs"
    paths = []
    if runs.is_dir() and not runs.is_symlink():
        try:
            candidates = list(runs.glob("*/*benchmark*.json"))
        except OSError:
            candidates = []
        safe_candidates = []
        for path in candidates:
            try:
                stamp = path.stat().st_mtime_ns
            except OSError:
                continue
            safe_candidates.append((stamp, path))
        paths = [
            path for _, path in sorted(safe_candidates, reverse=True)[:MAX_HISTORY_REPORTS]
        ]

    for path in paths:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_HISTORY_BYTES:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, Mapping) or suite_from_report(value) != suite:
            continue
        reports += 1
        report_sha = _text(value.get("code_sha256"))
        if report_sha != current_sha:
            stale_or_unknown += 1
            continue

        passed = value.get("passed")
        failed = value.get("failed")
        if (
            type(passed) is not int
            or passed < 0
            or type(failed) is not int
            or failed < 0
        ):
            continue

        current_reports += 1
        if failed > 0:
            current_failures += 1
        else:
            current_passes += 1
        if latest_current is None:
            latest_current = {
                "suite": suite,
                "suite_id": _text(value.get("suite_id")),
                "passed": passed,
                "failed": failed,
                "artifact": str(path),
                "current_code_match": True,
            }

    latest_failed = (
        int(latest_current["failed"])
        if latest_current is not None and type(latest_current.get("failed")) is int
        else 0
    )
    recurring = bool(
        current_failures >= RECURRENCE_THRESHOLD
        and latest_current is not None
        and latest_failed > 0
    )
    return {
        "suite": suite,
        "reports_seen": reports,
        "current_code_reports": current_reports,
        "current_code_failed_reports": current_failures,
        "current_code_passed_reports": current_passes,
        "stale_or_unknown_reports": stale_or_unknown,
        "latest_current": latest_current,
        "recurring_weakness": recurring,
        "recurrence_threshold": RECURRENCE_THRESHOLD,
        "raw_output_included": False,
    }


def select_benchmark_suite(
    target: Mapping[str, object],
    *,
    root: Path | None = None,
) -> dict[str, object]:
    """Select one already-supported suite without inventing a capability."""
    if not isinstance(target, Mapping):
        raise ValueError("benchmark target must be a mapping")

    explicit = _text(target.get("benchmark_suite"))
    if explicit is not None:
        if explicit not in SUPPORTED_BENCHMARKS:
            raise ValueError("Unsupported experiment benchmark suite")
        suite, source = explicit, "explicit"
    else:
        paths = (
            _text(target.get("path")),
            _nested_text(target, "target", "path"),
            _nested_text(target, "opportunity_context", "path"),
            _nested_text(target, "memory_snapshot", "path"),
        )
        suite = next((found for found in (_suite_from_path(p) for p in paths) if found), None)
        source = "path" if suite is not None else "none"

        if suite is None:
            capabilities = (
                _text(target.get("capability")),
                _nested_text(target, "memory_snapshot", "capability"),
                _nested_text(target, "target", "capability"),
            )
            suite = next(
                (found for found in (_suite_from_capability(c) for c in capabilities) if found),
                None,
            )
            source = "capability" if suite is not None else "none"

    history = (
        benchmark_history_summary(root, suite)
        if root is not None and suite is not None
        else None
    )
    return {
        "schema": "sira.auto_benchmark_selection.v1",
        "policy_version": AUTO_EVALUATOR_POLICY_VERSION,
        "suite": suite,
        "selection_source": source,
        "cli_args": list(SUPPORTED_BENCHMARKS[suite]) if suite is not None else [],
        "supported": suite is not None,
        "history": history,
        "authority_granted": False,
        "promotion_authorized": False,
    }


def benchmark_evidence_for_target(
    root: Path,
    target: Mapping[str, object],
) -> dict[str, object]:
    """Convert local benchmark history into bounded non-authoritative evidence."""
    selection = select_benchmark_suite(target, root=Path(root).resolve())
    suite = selection.get("suite")
    history = selection.get("history")
    if suite is None or not isinstance(history, Mapping):
        classification = "no_supported_benchmark"
        priority_boost = 0
    else:
        latest = history.get("latest_current")
        latest_failed = (
            int(latest.get("failed"))
            if isinstance(latest, Mapping) and type(latest.get("failed")) is int
            else 0
        )
        failed_reports = int(history.get("current_code_failed_reports") or 0)
        if history.get("recurring_weakness") is True:
            classification = "recurring_current_regression"
            priority_boost = min(
                MAX_BENCHMARK_PRIORITY_BOOST,
                60 + failed_reports * 30 + min(latest_failed * 10, 40),
            )
        elif latest_failed > 0:
            classification = "one_off_current_regression"
            priority_boost = 0
        elif int(history.get("current_code_reports") or 0) > 0:
            classification = "current_clean"
            priority_boost = 0
        elif int(history.get("stale_or_unknown_reports") or 0) > 0:
            classification = "stale_or_unknown_only"
            priority_boost = 0
        else:
            classification = "no_current_evidence"
            priority_boost = 0

    return {
        "schema": "sira.auto_evaluator_target_evidence.v1",
        "policy_version": AUTO_EVALUATOR_POLICY_VERSION,
        "suite": suite,
        "selection_source": selection.get("selection_source"),
        "classification": classification,
        "priority_boost": priority_boost,
        "history": history,
        "raw_output_included": False,
        "authority_granted": False,
        "promotion_authorized": False,
        "external_access_requested": False,
    }


def normalize_health(value: Mapping[str, object] | None) -> dict[str, object]:
    tests = value.get("tests") if isinstance(value, Mapping) else None
    benchmark = value.get("benchmark") if isinstance(value, Mapping) else None

    test_count = tests.get("test_count") if isinstance(tests, Mapping) else None
    passed_cases = benchmark.get("passed_cases") if isinstance(benchmark, Mapping) else None
    failed_cases = benchmark.get("failed_cases") if isinstance(benchmark, Mapping) else None

    return {
        "overall_passed": (
            value.get("overall_passed") is True if isinstance(value, Mapping) else False
        ),
        "tests_passed": tests.get("passed") is True if isinstance(tests, Mapping) else None,
        "test_count": test_count if type(test_count) is int and test_count >= 0 else None,
        "benchmark_present": isinstance(benchmark, Mapping),
        "benchmark_passed": (
            benchmark.get("passed") is True if isinstance(benchmark, Mapping) else None
        ),
        "passed_cases": (
            passed_cases if type(passed_cases) is int and passed_cases >= 0 else None
        ),
        "failed_cases": (
            failed_cases if type(failed_cases) is int and failed_cases >= 0 else None
        ),
    }


def compare_health(
    baseline_health: Mapping[str, object] | None,
    candidate_health: Mapping[str, object] | None,
) -> dict[str, object]:
    baseline = normalize_health(baseline_health)
    candidate = normalize_health(candidate_health)

    if baseline["overall_passed"] is not True:
        classification = "baseline_unhealthy"
        not_weaker = False
    elif candidate["overall_passed"] is not True:
        classification = "candidate_regression"
        not_weaker = False
    else:
        measurable = all(
            type(value) is int
            for value in (
                baseline["test_count"],
                candidate["test_count"],
                baseline["passed_cases"],
                candidate["passed_cases"],
                baseline["failed_cases"],
                candidate["failed_cases"],
            )
        )
        if not measurable:
            classification = "verification_unmeasured"
            not_weaker = None
        else:
            weaker = bool(
                candidate["test_count"] < baseline["test_count"]
                or candidate["passed_cases"] < baseline["passed_cases"]
                or candidate["failed_cases"] > baseline["failed_cases"]
            )
            expanded = bool(
                candidate["test_count"] > baseline["test_count"]
                or candidate["passed_cases"] > baseline["passed_cases"]
                or candidate["failed_cases"] < baseline["failed_cases"]
            )
            if weaker:
                classification = "verification_shrinkage"
                not_weaker = False
            elif expanded:
                classification = "verification_expanded"
                not_weaker = True
            else:
                classification = "verification_equivalent"
                not_weaker = True

    return {
        "schema": "sira.auto_evaluator_comparison.v1",
        "policy_version": AUTO_EVALUATOR_POLICY_VERSION,
        "classification": classification,
        "verification_not_weaker": not_weaker,
        "baseline": baseline,
        "candidate": candidate,
        "raw_output_included": False,
        "promotion_authorized": False,
    }
