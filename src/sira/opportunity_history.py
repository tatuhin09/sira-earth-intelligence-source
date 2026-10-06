"""Bounded local history signals for opportunity ranking.

This module reads only existing local benchmark artifacts and learning-memory
rows. History is ranking evidence only and can never authorize a code change.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from .auto_evaluator import SUPPORTED_BENCHMARKS, benchmark_history_summary

MAX_BENCHMARK_ARTIFACTS = 500
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_MEMORY_DB_BYTES = 64 * 1024 * 1024
MIN_PROVIDER_FAILURES = 2
MIN_PROVIDER_SAMPLES = 3
MIN_PROVIDER_FAILURE_RATE = 0.50

FILE_HISTORY_MAP: dict[str, tuple[str, str]] = {
    "retrieval.py": ("retrieval", "retrieval"),
    "reading.py": ("reading", "reading"),
    "synthesis.py": ("synthesis", "synthesis"),
    "papers.py": ("scholarly_research", "papers"),
    "paper_reading.py": ("paper_reading", "paper-reading"),
    "memory.py": ("memory", "memory"),
    "improvement.py": ("improvement", "improvement"),
    "opportunity.py": ("improvement", "improvement"),
    "opportunity_detectors.py": ("improvement", "improvement"),
    "opportunity_history.py": ("improvement", "improvement"),
    "code_writer.py": ("improvement", "improvement"),
    "evaluator2.py": ("improvement", "improvement"),
    "auto_evaluator.py": ("improvement", "improvement"),
    "autonomous_targeting.py": ("improvement", "improvement"),
}


def project_code_digest(root: Path) -> str:
    """Backward-compatible digest helper used by historical tests/callers."""
    source_root = Path(root).resolve() / "src" / "sira"
    digest = hashlib.sha256()
    if not source_root.is_dir():
        return digest.hexdigest()
    for path in sorted(source_root.rglob("*.py")):
        if path.is_symlink() or not path.is_file():
            continue
        digest.update(
            str(path.relative_to(source_root)).encode()
            + b"\0"
            + path.read_bytes()
            + b"\0"
        )
    return digest.hexdigest()


def _safe_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _benchmark_history(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Aggregate current-code recurrence instead of trusting a one-off report."""
    latest: dict[str, dict[str, Any]] = {}
    metrics = {
        "current_benchmark_reports": 0,
        "current_benchmark_suites": 0,
        "stale_benchmark_reports_ignored": 0,
        "recurring_benchmark_weakness_count": 0,
    }
    for suite in SUPPORTED_BENCHMARKS:
        summary = benchmark_history_summary(root, suite)
        metrics["current_benchmark_reports"] += int(summary["current_code_reports"])
        metrics["stale_benchmark_reports_ignored"] += int(summary["stale_or_unknown_reports"])
        current = summary.get("latest_current")
        if not isinstance(current, dict):
            continue
        metrics["current_benchmark_suites"] += 1
        recurring = summary.get("recurring_weakness") is True
        metrics["recurring_benchmark_weakness_count"] += int(recurring)
        latest[suite] = {
            **current,
            "current_report_count": int(summary["current_code_reports"]),
            "current_failed_reports": int(summary["current_code_failed_reports"]),
            "current_passed_reports": int(summary["current_code_passed_reports"]),
            "recurring_weakness": recurring,
            "recurrence_threshold": int(summary["recurrence_threshold"]),
            "authority_granted": False,
            "promotion_authorized": False,
        }
    return latest, metrics


def _provider_history(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    db = Path(root) / "memory" / "sira_memory.sqlite3"
    metrics = {"provider_history_rows": 0, "recurring_unreliable_provider_count": 0}
    if db.is_symlink() or not db.is_file():
        return {}, metrics
    try:
        if db.stat().st_size > MAX_MEMORY_DB_BYTES:
            return {}, metrics
        uri = f"file:{db.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
            rows = conn.execute(
                "SELECT capability,provider,kind,category,occurrence_count "
                "FROM memories WHERE provider IS NOT NULL AND occurrence_count > 0"
            ).fetchall()
    except (OSError, sqlite3.DatabaseError):
        return {}, metrics

    metrics["provider_history_rows"] = len(rows)
    aggregates: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"successes": 0, "failures": 0}
    )
    synthetic_markers = ("fixture", "mock", "dummy", "synthetic")
    for capability, provider, kind, category, occurrence_count in rows:
        if not isinstance(capability, str) or not isinstance(provider, str):
            continue
        normalized_provider = " ".join(provider.lower().replace("_", " ").split())
        if any(marker in normalized_provider for marker in synthetic_markers):
            continue
        count = int(occurrence_count or 0)
        if count <= 0:
            continue
        bucket = aggregates[(capability, provider)]
        if kind == "success" or category == "success":
            bucket["successes"] += count
        elif kind in {"failure", "rejection"}:
            bucket["failures"] += count

    by_capability: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (capability, provider), counts in aggregates.items():
        failures = counts["failures"]
        successes = counts["successes"]
        total = failures + successes
        if total <= 0:
            continue
        rate = failures / total
        if (
            failures < MIN_PROVIDER_FAILURES
            or total < MIN_PROVIDER_SAMPLES
            or rate < MIN_PROVIDER_FAILURE_RATE
        ):
            continue
        by_capability[capability].append({
            "provider": provider,
            "failures": failures,
            "successes": successes,
            "samples": total,
            "failure_rate": round(rate, 4),
        })

    result: dict[str, dict[str, Any]] = {}
    for capability, items in by_capability.items():
        items.sort(key=lambda row: (-row["failures"], -row["failure_rate"], row["provider"]))
        result[capability] = {
            "capability": capability,
            "recurring_unreliable_providers": items[:10],
            "minimum_recurrence_enforced": True,
            "minimum_failures": MIN_PROVIDER_FAILURES,
            "minimum_samples": MIN_PROVIDER_SAMPLES,
            "minimum_failure_rate": MIN_PROVIDER_FAILURE_RATE,
        }
        metrics["recurring_unreliable_provider_count"] += len(items)
    return result, metrics


def collect_history_context(root: Path) -> dict[str, Any]:
    """Collect bounded local history used only for opportunity ranking."""
    root = Path(root).resolve()
    benchmarks, benchmark_metrics = _benchmark_history(root)
    providers, provider_metrics = _provider_history(root)
    return {
        "benchmarks": benchmarks,
        "provider_reliability": providers,
        "metrics": {**benchmark_metrics, **provider_metrics},
        "api_requests": 0,
        "paid_spending": False,
        "authority_granted": False,
    }


def context_for_path(history: dict[str, Any], rel: str) -> dict[str, Any]:
    capability, suite = FILE_HISTORY_MAP.get(Path(rel).name, (None, None))
    benchmark = history.get("benchmarks", {}).get(suite) if suite else None
    provider = history.get("provider_reliability", {}).get(capability) if capability else None
    return {
        "benchmark": benchmark or {
            "suite": suite,
            "current_code_match": False,
            "passed": None,
            "failed": None,
            "current_report_count": 0,
            "current_failed_reports": 0,
            "current_passed_reports": 0,
            "recurring_weakness": False,
            "recurrence_threshold": 2,
            "authority_granted": False,
            "promotion_authorized": False,
        },
        "provider_reliability": provider or {
            "capability": capability,
            "recurring_unreliable_providers": [],
            "minimum_recurrence_enforced": True,
            "minimum_failures": MIN_PROVIDER_FAILURES,
            "minimum_samples": MIN_PROVIDER_SAMPLES,
            "minimum_failure_rate": MIN_PROVIDER_FAILURE_RATE,
        },
    }
