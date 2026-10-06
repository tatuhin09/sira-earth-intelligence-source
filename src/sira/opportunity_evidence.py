"""Local evidence enrichment for deterministic improvement opportunities.

v1.0C-3b turns a static opportunity signal into a bounded research brief.
It performs no network/model calls and never edits the main source tree.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from .memory import MemoryStore
from .engineering_intelligence import inspect_project, plan_verification
from .engineering_verification import verification_execution_contract
from .models import utc_now
from .self_modification import PROTECTED_PATHS
from .storage import write_json

EVIDENCE_POLICY_VERSION = 2
OPPORTUNITY_ID_RE = re.compile(r"op_[0-9a-f]{32}\Z")
MAX_SCAN_ARTIFACTS = 200
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024
MAX_EXCERPT_BYTES = 64 * 1024
MAX_CALLERS = 20
MAX_TEST_REFERENCES = 20
MAX_RELATED_MEMORIES = 5

# Capability mapping is intentionally explicit. Unknown internal modules are not
# automatically sent to the writer because they lack a deterministic benchmark.
_FILE_SUITE_MAP: dict[str, tuple[str, str]] = {
    "retrieval.py": ("retrieval", "retrieval"),
    "reading.py": ("reading", "reading"),
    "synthesis.py": ("synthesis", "synthesis"),
    "papers.py": ("scholarly_research", "papers"),
    "paper_reading.py": ("paper_reading", "paper-reading"),
    "memory.py": ("memory", "memory"),
    "improvement.py": ("improvement", "improvement"),
    "opportunity.py": ("improvement", "improvement"),
    "opportunity_detectors.py": ("improvement", "improvement"),
    "code_writer.py": ("improvement", "improvement"),
    "evaluator2.py": ("improvement", "improvement"),
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bounded_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _safe_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _load_opportunity(root: Path, opportunity_id: str) -> dict[str, Any]:
    if not isinstance(opportunity_id, str) or not OPPORTUNITY_ID_RE.fullmatch(opportunity_id):
        raise ValueError("Invalid opportunity ID")
    scans = root / "improvements" / "opportunities" / "scans"
    if not scans.is_dir():
        raise ValueError("Opportunity not found; run improve opportunities first")
    paths = sorted(scans.glob("od_*.json"), key=lambda p: p.stat().st_mtime_ns if p.exists() else 0, reverse=True)
    for path in paths[:MAX_SCAN_ARTIFACTS]:
        report = _safe_json(path)
        if report is None or report.get("kind") != "opportunity_discovery":
            continue
        rows = report.get("opportunities")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("opportunity_id") == opportunity_id:
                return dict(row)
    raise ValueError("Opportunity not found; run improve opportunities first")


def _target_source(root: Path, opportunity: dict[str, Any]) -> tuple[Path, bytes, str]:
    rel = opportunity.get("path")
    if not isinstance(rel, str) or rel in PROTECTED_PATHS or not rel.startswith("src/sira/"):
        raise ValueError("Opportunity target path is not modifiable")
    path = (root / rel).resolve()
    public_root = (root / "src" / "sira").resolve()
    try:
        path.relative_to(public_root)
    except ValueError as exc:
        raise ValueError("Opportunity target path escapes modifiable source") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError("Opportunity target source is unavailable")
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("Opportunity target source is too large")
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Opportunity target source is not UTF-8 text") from exc
    if _sha256_bytes(payload) != opportunity.get("source_sha256"):
        raise ValueError("Opportunity is stale because target source changed")
    return path, payload, text


def _target_excerpt(rel: str, text: str, opportunity: dict[str, Any]) -> dict[str, Any]:
    symbol = opportunity.get("symbol")
    evidence = opportunity.get("evidence") if isinstance(opportunity.get("evidence"), dict) else {}
    lines = text.splitlines()
    start = 1
    end = min(len(lines), 160)
    if isinstance(symbol, str) and symbol:
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError:
            tree = None
        if tree is not None:
            expected_line = evidence.get("line")
            matches = [node for node in ast.walk(tree)
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol]
            if expected_line is not None:
                matches.sort(key=lambda node: abs(int(node.lineno) - int(expected_line)))
            if matches:
                node = matches[0]
                start = int(node.lineno)
                end = int(getattr(node, "end_lineno", node.lineno))
    elif opportunity.get("type") == "syntax_parse_failure":
        line = evidence.get("line")
        if isinstance(line, int):
            start = max(1, line - 20)
            end = min(len(lines), line + 20)
    excerpt = "\n".join(lines[start - 1:end])
    excerpt = _bounded_utf8(excerpt, MAX_EXCERPT_BYTES)
    return {
        "path": rel,
        "symbol": symbol,
        "line": start,
        "end_line": end,
        "source_excerpt": excerpt,
        "source_excerpt_sha256": _sha256_bytes(excerpt.encode("utf-8")),
    }


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _find_callers(root: Path, target_rel: str, symbol: str | None) -> list[dict[str, Any]]:
    if not symbol:
        return []
    out: list[dict[str, Any]] = []
    source_root = root / "src" / "sira"
    for path in sorted(source_root.rglob("*.py")):
        if len(out) >= MAX_CALLERS:
            break
        rel = path.relative_to(root).as_posix()
        if rel in PROTECTED_PATHS or rel == target_rel or path.is_symlink():
            continue
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=rel)
        except (OSError, UnicodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == symbol:
                out.append({"path": rel, "line": int(node.lineno), "symbol": symbol})
                if len(out) >= MAX_CALLERS:
                    break
    return out


def _find_tests(root: Path, target_rel: str, symbol: str | None) -> list[dict[str, Any]]:
    tests_root = root / "tests"
    if not tests_root.is_dir():
        return []
    module_stem = Path(target_rel).stem
    out: list[dict[str, Any]] = []
    for path in sorted(tests_root.rglob("*.py")):
        if len(out) >= MAX_TEST_REFERENCES:
            break
        if path.is_symlink():
            continue
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=path.as_posix())
        except (OSError, UnicodeError, SyntaxError):
            continue
        matching_lines: list[int] = []
        for node in ast.walk(tree):
            matched = False
            if symbol and isinstance(node, ast.Name) and node.id == symbol:
                matched = True
            elif symbol and isinstance(node, ast.Attribute) and node.attr == symbol:
                matched = True
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.endswith(module_stem) or any(alias.name == symbol for alias in node.names if symbol):
                    matched = True
            elif isinstance(node, ast.Import):
                if any(alias.name.endswith("." + module_stem) or alias.name == module_stem for alias in node.names):
                    matched = True
            if matched and hasattr(node, "lineno"):
                matching_lines.append(int(node.lineno))
        matching_lines = sorted(set(matching_lines))
        if matching_lines:
            out.append({
                "path": path.relative_to(root).as_posix(),
                "matching_lines": matching_lines[:8],
                "match_count": len(matching_lines),
            })
    return out


def _suite_for(target_rel: str) -> dict[str, Any]:
    mapped = _FILE_SUITE_MAP.get(Path(target_rel).name)
    if mapped is None:
        return {"capability": None, "suite": None, "mapping": "unmapped"}
    capability, suite = mapped
    return {"capability": capability, "suite": suite, "mapping": "explicit_file_map"}


def opportunity_readiness_preview(root: Path, opportunity: dict[str, Any]) -> dict[str, Any]:
    """Check mandatory local evidence before scheduling an opportunity cycle.

    This is only a scheduling hint. The full brief still rechecks the exact
    source and all evidence before research or a writer can run.
    """
    path = opportunity.get("path")
    symbol = opportunity.get("symbol")
    kind = opportunity.get("type")
    if not isinstance(path, str) or not path.startswith("src/sira/"):
        return {"ready": False, "reason": "invalid_target"}
    if _suite_for(path)["suite"] is None:
        return {"ready": False, "reason": "benchmark_unmapped"}
    if kind == "missing_test_reference":
        if not _find_callers(root, path, symbol):
            return {"ready": False, "reason": "caller_missing"}
    elif not _find_tests(root, path, symbol):
        return {"ready": False, "reason": "test_reference_missing"}
    return {"ready": True, "reason": "mandatory_evidence_present"}


def _related_memories(root: Path, capability: str | None) -> list[dict[str, Any]]:
    if not capability or not (root / "memory" / "sira_memory.sqlite3").is_file():
        return []
    store = MemoryStore(root)
    rows = store.retrieve(capability, 20, autonomous=True)[:MAX_RELATED_MEMORIES]
    for row in rows:
        store.record_outcome(
            row["memory_id"],
            "retrieval_used",
            context={
                "consumer": "opportunity_evidence._related_memories",
                "query": capability,
            },
            weight=1.0,
        )
    return [{key: row.get(key) for key in (
        "memory_id", "kind", "status", "category", "capability", "provider", "error_code",
        "summary", "occurrence_count", "retrieval_score", "score_reasons"
    )} for row in rows]


def _structural_goal(opportunity: dict[str, Any]) -> dict[str, Any]:
    kind = opportunity.get("type")
    evidence = opportunity.get("evidence") if isinstance(opportunity.get("evidence"), dict) else {}
    if kind == "complex_function" and isinstance(evidence.get("branch_points"), int):
        baseline = int(evidence["branch_points"])
        reduction = max(2, math.ceil(baseline * 0.10))
        return {"metric": "branch_points", "baseline": baseline, "target_max": max(0, baseline - reduction)}
    if kind == "missing_test_reference":
        baseline = int(evidence.get("test_reference_count", 0))
        return {"metric": "test_calls", "baseline": baseline, "target_min": max(1, baseline + 1)}
    if kind == "weak_error_handling" and isinstance(evidence.get("weak_exception_handlers"), int):
        baseline = int(evidence["weak_exception_handlers"])
        return {"metric": "weak_exception_handlers", "baseline": baseline, "target_max": max(0, baseline - 1)}
    if kind == "duplicate_function_body" and isinstance(evidence.get("duplicate_body_matches"), int):
        baseline = int(evidence["duplicate_body_matches"])
        return {"metric": "duplicate_body_matches", "baseline": baseline, "target_max": max(0, baseline - 1)}
    if kind == "large_module" and isinstance(evidence.get("line_count"), int):
        baseline = int(evidence["line_count"])
        return {"metric": "line_count", "baseline": baseline, "target_max": max(1, math.floor(baseline * 0.90))}
    if kind == "explicit_debt_marker" and isinstance(evidence.get("marker_count"), int):
        baseline = int(evidence["marker_count"])
        return {"metric": "debt_markers", "baseline": baseline, "target_max": max(0, baseline - 1)}
    if kind == "syntax_parse_failure":
        return {"metric": "parse_failures", "baseline": 1, "target_max": 0}
    return {"metric": "static_signal", "baseline": 1, "target_max": 0}


def _assessment(opportunity: dict[str, Any], callers: list[dict[str, Any]], tests: list[dict[str, Any]],
                related: list[dict[str, Any]], benchmark: dict[str, Any]) -> dict[str, Any]:
    kind = opportunity.get("type")
    score = 2  # deterministic local signal itself
    reasons = ["deterministic_static_signal"]
    if benchmark.get("suite"):
        score += 2
        reasons.append("relevant_benchmark_mapped")
    if tests:
        score += 2
        reasons.append("existing_tests_reference_target")
    if callers:
        score += 1
        reasons.append("callers_identified")
    if related:
        score += 1
        reasons.append("related_learning_memory_found")
    if kind in {"syntax_parse_failure", "explicit_debt_marker", "missing_test_reference",
                "weak_error_handling", "duplicate_function_body"}:
        score += 1
        reasons.append("explicit_quality_signal")

    if kind == "missing_test_reference":
        # Absence of a direct test is the signal, so requiring a pre-existing
        # test would make this opportunity impossible to validate. A mapped
        # benchmark plus a real caller provides the minimum behavioral oracle.
        ready = bool(benchmark.get("suite") and callers and score >= 6)
        requires_test_reference = False
        requires_caller = True
    else:
        ready = bool(benchmark.get("suite") and tests and score >= 6)
        requires_test_reference = True
        requires_caller = False

    return {
        "decision": "research_ready" if ready else "skip_weak_evidence",
        "evidence_score": score,
        "threshold": 6,
        "reasons": reasons,
        "requires_benchmark_mapping": True,
        "requires_test_reference": requires_test_reference,
        "requires_caller": requires_caller,
    }


def build_opportunity_evidence(root: Path, opportunity_id: str) -> dict[str, Any]:
    """Build a zero-network evidence brief for one persisted opportunity."""
    root = Path(root).resolve()
    opportunity = _load_opportunity(root, opportunity_id)
    _, _, text = _target_source(root, opportunity)
    target_rel = str(opportunity["path"])
    target = _target_excerpt(target_rel, text, opportunity)
    callers = _find_callers(root, target_rel, opportunity.get("symbol"))
    tests = _find_tests(root, target_rel, opportunity.get("symbol"))
    benchmark = _suite_for(target_rel)
    engineering_profile = inspect_project(root)
    verification_plan = plan_verification(
        root,
        profile=engineering_profile,
        target_path=target_rel,
    )
    execution_contract = verification_execution_contract(
        verification_plan
    )
    related = _related_memories(root, benchmark.get("capability"))
    assessment = _assessment(opportunity, callers, tests, related, benchmark)
    research_ready = assessment["decision"] == "research_ready"

    success_criteria = {
        "research_ready": research_ready,
        "preserve_current_observable_behavior": True,
        "unit_tests_must_pass": True,
        "test_count_must_not_decrease": True,
        "benchmark_suite": benchmark.get("suite"),
        "relevant_benchmark_must_pass": bool(benchmark.get("suite")),
        "protected_shell_must_remain_unchanged": True,
        "structural_goal": _structural_goal(opportunity),
    }
    evidence_id = "oe_" + uuid4().hex
    report = {
        "schema_version": 1,
        "kind": "opportunity_evidence_brief",
        "policy_version": EVIDENCE_POLICY_VERSION,
        "evidence_id": evidence_id,
        "created_at": utc_now(),
        "opportunity": opportunity,
        "target": target,
        "callers": callers,
        "tests": tests,
        "related_memories": related,
        "benchmark": benchmark,
        "engineering_profile": engineering_profile,
        "verification_plan": verification_plan,
        "verification_execution_contract": execution_contract,
        "assessment": assessment,
        "success_criteria": success_criteria,
        "research_policy": (
            "local/free-first evidence enrichment; static signal alone cannot authorize a code change; "
            "each signal type must satisfy its explicit behavioral-oracle requirements before writer handoff; "
            "engineering profile and verification plan are read-only advisory context and cannot authorize writer or promotion"
        ),
        "api_requests": 0,
        "paid_spending": False,
    }
    artifact = root / "improvements" / "opportunities" / "evidence" / f"{evidence_id}.json"
    report["artifact"] = str(artifact)
    write_json(artifact, report)
    return report
