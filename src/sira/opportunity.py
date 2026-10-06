"""Deterministic local improvement-opportunity discovery.

v1.0C-3a intentionally performs no network or model calls. It scans only
modifiable Python source, skips the protected shell, ranks evidence-backed
static signals, and keeps attempt cooldown state separate from discovery.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from .models import utc_now
from .opportunity_detectors import collect_test_identifiers, detect_function_signals
from .opportunity_history import collect_history_context, context_for_path
from .self_modification import PROTECTED_PATHS
from .storage import write_json

OPPORTUNITY_POLICY_VERSION = 3
FINGERPRINT_POLICY_VERSION = 1
DEFAULT_COOLDOWN_SECONDS = 6 * 60 * 60
MAX_SOURCE_FILES = 500
MAX_SOURCE_FILE_BYTES = 512 * 1024
MAX_HISTORY_ENTRIES = 5000
MAX_DISCOVERY_LIMIT = 80
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")
_MARKER_RE = re.compile(r"\b(TODO|FIXME|XXX)\b", re.IGNORECASE)


class OpportunityStoreError(ValueError):
    pass


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _source_sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fingerprint(kind: str, path: str, symbol: str | None, source_sha256: str, evidence: dict[str, Any]) -> str:
    payload = {
        "policy_version": FINGERPRINT_POLICY_VERSION,
        "type": kind,
        "path": path,
        "symbol": symbol,
        "source_sha256": source_sha256,
        "evidence": evidence,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _opportunity(kind: str, path: str, symbol: str | None, source_sha256: str,
                 summary: str, evidence: dict[str, Any], priority_score: int) -> dict[str, Any]:
    fingerprint = _fingerprint(kind, path, symbol, source_sha256, evidence)
    return {
        "opportunity_id": "op_" + fingerprint[:32],
        "fingerprint": fingerprint,
        "type": kind,
        "path": path,
        "symbol": symbol,
        "summary": summary,
        "evidence": evidence,
        "priority_score": int(priority_score),
        "source_sha256": source_sha256,
    }



class OpportunityStore:
    """Persist bounded attempt history and discovery audit artifacts."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.base = self.root / "improvements" / "opportunities"
        self.history_path = self.base / "attempt_history.json"
        self.scans = self.base / "scans"

    def _load_history(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.history_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OpportunityStoreError("Opportunity attempt history is corrupt") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("attempts"), dict):
            raise OpportunityStoreError("Opportunity attempt history format is invalid")
        attempts = raw["attempts"]
        if len(attempts) > MAX_HISTORY_ENTRIES:
            raise OpportunityStoreError("Opportunity attempt history is too large")
        for fingerprint, item in attempts.items():
            if (not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint)
                    or not isinstance(item, dict) or not isinstance(item.get("attempted_at_epoch"), (int, float))):
                raise OpportunityStoreError("Opportunity attempt history contains invalid entries")
        return attempts

    def mark_attempt(self, opportunity: dict[str, Any], outcome: str, *, attempted_at_epoch: float | None = None) -> dict[str, Any]:
        if not isinstance(opportunity, dict):
            raise OpportunityStoreError("Opportunity must be an object")
        fingerprint = opportunity.get("fingerprint")
        if not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint):
            raise OpportunityStoreError("Opportunity fingerprint is invalid")
        if not isinstance(outcome, str) or not 1 <= len(outcome.strip()) <= 120:
            raise OpportunityStoreError("Opportunity outcome must be 1..120 characters")
        when = time.time() if attempted_at_epoch is None else attempted_at_epoch
        if not isinstance(when, (int, float)) or isinstance(when, bool) or when < 0:
            raise OpportunityStoreError("attempted_at_epoch must be a non-negative number")

        attempts = self._load_history()
        attempts[fingerprint] = {
            "opportunity_id": opportunity.get("opportunity_id"),
            "type": opportunity.get("type"),
            "path": opportunity.get("path"),
            "symbol": opportunity.get("symbol"),
            "outcome": outcome.strip(),
            "attempted_at_epoch": float(when),
            "attempted_at": _iso_from_epoch(float(when)),
        }
        if len(attempts) > MAX_HISTORY_ENTRIES:
            ordered = sorted(attempts.items(), key=lambda item: item[1]["attempted_at_epoch"], reverse=True)
            attempts = dict(ordered[:MAX_HISTORY_ENTRIES])
        payload = {"schema_version": 1, "policy_version": OPPORTUNITY_POLICY_VERSION,
                   "updated_at": utc_now(), "attempts": attempts}
        write_json(self.history_path, payload)
        return attempts[fingerprint]

    def cooldown_state(self, fingerprint: str, *, now_epoch: float, cooldown_seconds: int) -> dict[str, Any]:
        attempts = self._load_history()
        item = attempts.get(fingerprint)
        if item is None:
            return {"eligible": True, "last_attempt_at": None, "remaining_seconds": 0}
        elapsed = max(0.0, float(now_epoch) - float(item["attempted_at_epoch"]))
        remaining = max(0, int(cooldown_seconds - elapsed))
        return {
            "eligible": elapsed >= cooldown_seconds,
            "last_attempt_at": item.get("attempted_at"),
            "last_outcome": item.get("outcome"),
            "remaining_seconds": remaining,
        }

    def save_scan(self, report: dict[str, Any]) -> Path:
        scan_id = report.get("scan_id")
        if not isinstance(scan_id, str) or not scan_id.startswith("od_"):
            raise OpportunityStoreError("Opportunity discovery scan ID is invalid")
        path = self.scans / f"{scan_id}.json"
        write_json(path, report)
        return path


def _scan_source(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_root = root / "src" / "sira"
    opportunities: list[dict[str, Any]] = []
    metrics = {
        "source_files_scanned": 0,
        "protected_files_skipped": 0,
        "symlinks_skipped": 0,
        "oversized_files_skipped": 0,
        "parse_failures": 0,
    }
    if not source_root.is_dir():
        return opportunities, metrics

    test_identifiers = collect_test_identifiers(root, max_files=MAX_SOURCE_FILES, max_file_bytes=MAX_SOURCE_FILE_BYTES)
    detector_counts = {
        "complex_function": 0,
        "missing_test_reference": 0,
        "weak_error_handling": 0,
        "duplicate_function_body": 0,
    }
    paths = sorted(source_root.rglob("*.py"))[:MAX_SOURCE_FILES]
    for path in paths:
        rel = path.relative_to(root).as_posix()
        if rel in PROTECTED_PATHS:
            metrics["protected_files_skipped"] += 1
            continue
        if path.is_symlink():
            metrics["symlinks_skipped"] += 1
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_SOURCE_FILE_BYTES:
            metrics["oversized_files_skipped"] += 1
            continue
        try:
            payload = path.read_bytes()
            text = payload.decode("utf-8")
        except (OSError, UnicodeError):
            continue
        metrics["source_files_scanned"] += 1
        source_sha256 = _source_sha(payload)
        line_count = len(text.splitlines())

        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as error:
            metrics["parse_failures"] += 1
            evidence = {"line": error.lineno, "offset": error.offset, "line_count": line_count}
            opportunities.append(_opportunity(
                "syntax_parse_failure", rel, None, source_sha256,
                f"{rel} cannot be parsed as Python and needs deterministic repair.", evidence, 1000,
            ))
            continue

        marker_count = len(_MARKER_RE.findall(text))
        if marker_count:
            evidence = {"marker_count": marker_count, "line_count": line_count}
            opportunities.append(_opportunity(
                "explicit_debt_marker", rel, None, source_sha256,
                f"{rel} contains {marker_count} explicit TODO/FIXME/XXX marker(s).",
                evidence, 120 + min(marker_count, 20) * 12,
            ))

        if line_count >= 420:
            evidence = {"line_count": line_count, "threshold": 420}
            opportunities.append(_opportunity(
                "large_module", rel, None, source_sha256,
                f"{rel} is a large module ({line_count} lines) and may benefit from focused decomposition.",
                evidence, 70 + min(line_count, 1000) // 10,
            ))

        for signal in detect_function_signals(tree, rel, test_identifiers):
            kind = str(signal["kind"])
            if kind in detector_counts:
                detector_counts[kind] += 1
            opportunities.append(_opportunity(
                kind,
                rel,
                signal.get("symbol"),
                source_sha256,
                str(signal["summary"]),
                dict(signal["evidence"]),
                int(signal["priority_score"]),
            ))
    metrics["detector_signal_counts"] = detector_counts
    return opportunities, metrics


def discover_opportunities(root: Path, *, limit: int = 5,
                           cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
                           now_epoch: float | None = None) -> dict[str, Any]:
    """Discover ranked local opportunities without any provider/API calls."""
    root = Path(root).resolve()
    if type(limit) is not int or not 1 <= limit <= MAX_DISCOVERY_LIMIT:
        raise ValueError("Opportunity limit must be 1..80")
    if type(cooldown_seconds) is not int or cooldown_seconds < 0 or cooldown_seconds > 30 * 24 * 60 * 60:
        raise ValueError("Opportunity cooldown must be 0..2592000 seconds")
    when = time.time() if now_epoch is None else now_epoch
    if not isinstance(when, (int, float)) or isinstance(when, bool) or when < 0:
        raise ValueError("now_epoch must be a non-negative number")

    raw, scan_metrics = _scan_source(root)
    history = collect_history_context(root)

    # Cross-signal evidence is dynamic ranking context, not fingerprint input.
    # This preserves cooldown identity while letting independent evidence alter priority.
    grouped: dict[tuple[str, str | None], set[str]] = {}
    for row in raw:
        key = (str(row.get("path") or ""), row.get("symbol") if isinstance(row.get("symbol"), str) else None)
        grouped.setdefault(key, set()).add(str(row.get("type") or ""))

    for row in raw:
        base = int(row.get("priority_score") or 0)
        key = (str(row.get("path") or ""), row.get("symbol") if isinstance(row.get("symbol"), str) else None)
        signal_types = sorted(x for x in grouped.get(key, set()) if x)
        cross_count = len(signal_types)
        cross_boost = min(75, max(0, cross_count - 1) * 25)
        historical = context_for_path(history, str(row.get("path") or ""))
        benchmark = historical["benchmark"]
        failed = benchmark.get("failed")
        failed_reports = int(benchmark.get("current_failed_reports") or 0)
        benchmark_boost = 0
        if (
            benchmark.get("current_code_match") is True
            and benchmark.get("recurring_weakness") is True
            and isinstance(failed, int)
            and failed > 0
        ):
            benchmark_boost = min(180, 60 + failed_reports * 30 + min(failed * 10, 40))
        recurring = historical["provider_reliability"].get("recurring_unreliable_providers", [])
        total_failures = sum(int(item.get("failures") or 0) for item in recurring if isinstance(item, dict))
        provider_boost = min(80, len(recurring) * 20 + min(total_failures * 5, 40)) if recurring else 0
        final = base + cross_boost + benchmark_boost + provider_boost
        row["base_priority_score"] = base
        row["priority_score"] = final
        row["ranking"] = {
            "base_priority_score": base,
            "cross_signal_count": cross_count,
            "cross_signal_types": signal_types,
            "cross_signal_boost": cross_boost,
            "benchmark_boost": benchmark_boost,
            "benchmark_recurring_weakness": benchmark.get("recurring_weakness") is True,
            "benchmark_failed_reports": failed_reports,
            "provider_history_boost": provider_boost,
            "final_priority_score": final,
        }
        row["historical_context"] = historical

    raw.sort(key=lambda row: (-row["priority_score"], row["path"], row.get("symbol") or "", row["type"]))
    store = OpportunityStore(root)
    eligible: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for row in raw:
        item = dict(row)
        item["cooldown"] = store.cooldown_state(
            item["fingerprint"], now_epoch=float(when), cooldown_seconds=cooldown_seconds)
        if item["cooldown"]["eligible"]:
            eligible.append(item)
        else:
            suppressed.append(item)

    scan_id = "od_" + uuid4().hex
    report = {
        "schema_version": 1,
        "kind": "opportunity_discovery",
        "policy_version": OPPORTUNITY_POLICY_VERSION,
        "scan_id": scan_id,
        "created_at": utc_now(),
        "selection_policy": (
            "local-only deterministic multi-signal discovery with cross-signal ranking; protected shell excluded; "
            "recurring current-code benchmark weakness and recurring provider reliability history may raise priority "
            "but cannot authorize a change by themselves; stale or one-off benchmark failures and one-off provider "
            "failures do not boost ranking; "
            "identical recently attempted fingerprints are suppressed by cooldown"
        ),
        "cooldown_seconds": cooldown_seconds,
        "scan": {**scan_metrics, "history": history["metrics"], "raw_opportunities": len(raw)},
        "opportunities": eligible[:limit],
        "eligible_count": len(eligible),
        "suppressed_cooldown_count": len(suppressed),
        "api_requests": 0,
        "paid_spending": False,
    }
    artifact = store.save_scan(report)
    report["artifact"] = str(artifact)
    # Rewrite with the final self-reference so the persisted audit matches output.
    write_json(artifact, report)
    return report
