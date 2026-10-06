"""Protected orchestration for autonomous candidate verification and promotion.

The autonomous research/writer side may supply structured full-text candidate
edits, but only this gate applies them to an isolated candidate and invokes the
trusted Evaluator-2 -> Evaluator-1 -> transactional promotion chain.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Mapping, Protocol
from uuid import uuid4

from .evaluator1 import evaluate_final_gate
from .auto_evaluator import select_benchmark_suite
from .evaluator2 import evaluate_candidate
from .improvement import ExperimentRunner, ImprovementStore, SubprocessExperimentRunner
from .models import utc_now
from .promotion import promote_candidate
from .self_modification import CandidateEditError, ValidatedCandidateEditor, prepare_candidate_workspace
from .storage import write_json

AUTONOMOUS_PROMOTION_POLICY_VERSION = 1


def _load_symbol_tree(root: Path, relative: str, symbol: str) -> tuple[ast.Module, ast.FunctionDef | ast.AsyncFunctionDef]:
    if not isinstance(relative, str) or not relative.startswith("src/sira/"):
        raise ValueError("structural goal target path is invalid")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("structural goal target symbol is invalid")
    path = Path(root).resolve() / relative
    if path.is_symlink() or not path.is_file():
        raise ValueError("structural goal target is unavailable")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ValueError("structural goal target cannot be parsed") from exc
    matches = [node for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol]
    if len(matches) != 1:
        raise ValueError("structural goal target symbol is missing or ambiguous")
    return tree, matches[0]


def _branch_points_for_symbol(root: Path, relative: str, symbol: str) -> int:
    _, node = _load_symbol_tree(root, relative, symbol)
    score = 0
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.Match, ast.comprehension)):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += max(1, len(child.values) - 1)
    return score


def _attribute_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _target_module_name(relative: str) -> str:
    path = Path(relative)
    parts = list(path.with_suffix("").parts)
    if "src" not in parts:
        raise ValueError("structural goal target path is invalid")
    index = parts.index("src")
    module_parts = parts[index + 1:]
    if not module_parts:
        raise ValueError("structural goal target path is invalid")
    return ".".join(module_parts)


def _test_call_count(root: Path, relative: str, symbol: str) -> int:
    tests_root = Path(root).resolve() / "tests"
    if not tests_root.is_dir():
        return 0
    target_module = _target_module_name(relative)
    package, _, module_leaf = target_module.rpartition(".")
    count = 0
    for path in sorted(tests_root.rglob("*.py"))[:500]:
        if path.is_symlink():
            continue
        try:
            if path.stat().st_size > 512 * 1024:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        except (OSError, UnicodeError, SyntaxError):
            continue

        direct_names: set[str] = set()
        module_aliases: set[str] = set()
        unaliased_full_import = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == target_module:
                    for alias in node.names:
                        if alias.name == symbol:
                            direct_names.add(alias.asname or alias.name)
                elif package and module == package:
                    for alias in node.names:
                        if alias.name == module_leaf:
                            module_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name != target_module:
                        continue
                    if alias.asname:
                        module_aliases.add(alias.asname)
                    else:
                        unaliased_full_import = True

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in direct_names:
                count += 1
                continue
            if isinstance(func, ast.Attribute) and func.attr == symbol:
                if isinstance(func.value, ast.Name) and func.value.id in module_aliases:
                    count += 1
                    continue
                if unaliased_full_import and _attribute_chain(func) == f"{target_module}.{symbol}":
                    count += 1
    return count


def _is_weak_exception_handler(handler: ast.ExceptHandler) -> bool:
    broad = handler.type is None
    if isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}:
        broad = True
    if not broad or not handler.body:
        return False
    return all(isinstance(stmt, (ast.Pass, ast.Continue, ast.Break)) for stmt in handler.body)


def _weak_exception_handlers_for_symbol(root: Path, relative: str, symbol: str) -> int:
    _, node = _load_symbol_tree(root, relative, symbol)
    return sum(1 for child in ast.walk(node)
               if isinstance(child, ast.ExceptHandler) and _is_weak_exception_handler(child))


def _normalized_function_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    return ast.dump(ast.Module(body=body, type_ignores=[]), annotate_fields=True, include_attributes=False)


def _duplicate_body_matches_for_symbol(root: Path, relative: str, symbol: str) -> int:
    tree, node = _load_symbol_tree(root, relative, symbol)
    target_dump = _normalized_function_body(node)
    count = 0
    for peer in tree.body:
        if not isinstance(peer, (ast.FunctionDef, ast.AsyncFunctionDef)) or peer is node:
            continue
        if _normalized_function_body(peer) == target_dump:
            count += 1
    return count


def _measure_structural_metric(root: Path, relative: str, symbol: str, metric: str) -> int:
    if metric == "branch_points":
        return _branch_points_for_symbol(root, relative, symbol)
    if metric == "test_calls":
        # Validate the target symbol exists even though the measurement itself is in tests.
        _load_symbol_tree(root, relative, symbol)
        return _test_call_count(root, relative, symbol)
    if metric == "weak_exception_handlers":
        return _weak_exception_handlers_for_symbol(root, relative, symbol)
    if metric == "duplicate_body_matches":
        return _duplicate_body_matches_for_symbol(root, relative, symbol)
    raise ValueError("unsupported structural goal metric")


def _structural_goal_check(main_root: Path, candidate_root: Path, hypothesis: Mapping[str, object]) -> dict[str, object] | None:
    success = hypothesis.get("success_criteria")
    success = success if isinstance(success, Mapping) else {}
    goal = success.get("structural_goal")
    if not isinstance(goal, Mapping):
        return None
    metric = goal.get("metric")
    baseline = goal.get("baseline")
    target_max = goal.get("target_max")
    target_min = goal.get("target_min")
    target = hypothesis.get("target")
    target = target if isinstance(target, Mapping) else {}
    relative = target.get("path")
    symbol = target.get("symbol")
    if not isinstance(metric, str) or type(baseline) is not int:
        raise ValueError("unsupported or invalid structural goal")
    if baseline < 0:
        raise ValueError("structural goal values must be non-negative")
    has_max = type(target_max) is int
    has_min = type(target_min) is int
    if has_max == has_min:
        raise ValueError("structural goal requires exactly one integer target bound")
    bound = target_max if has_max else target_min
    if bound < 0:
        raise ValueError("structural goal values must be non-negative")

    main_value = _measure_structural_metric(main_root, relative, symbol, metric)
    candidate_value = _measure_structural_metric(candidate_root, relative, symbol, metric)
    baseline_matches = main_value == baseline
    achieved = candidate_value <= target_max if has_max else candidate_value >= target_min
    decision_code = (
        "structural_goal_met" if baseline_matches and achieved else
        "stale_structural_baseline" if not baseline_matches else
        "structural_goal_not_met"
    )
    result: dict[str, object] = {
        "metric": metric,
        "path": relative,
        "symbol": symbol,
        "expected_baseline": baseline,
        "baseline": main_value,
        "candidate": candidate_value,
        "baseline_matches": baseline_matches,
        "decision": "pass" if baseline_matches and achieved else "fail",
        "decision_code": decision_code,
    }
    if has_max:
        result["target_max"] = target_max
    else:
        result["target_min"] = target_min
    return result


class CandidateWriter(Protocol):
    def propose_text_edits(self, context: Mapping[str, object]) -> Mapping[str, str]: ...


class StructuredHypothesisWriter:
    """Read already-researched structured edits without executing generated code."""

    def propose_text_edits(self, context: Mapping[str, object]) -> Mapping[str, str]:
        edits = context.get("candidate_text_edits")
        return edits if isinstance(edits, Mapping) else {}


def _persist(workspace: Path, result: dict[str, object]) -> None:
    path = workspace / "autonomous_promotion.json"
    result["artifact"] = str(path)
    write_json(path, result)


def run_autonomous_candidate_promotion(
    root: Path,
    hypothesis: Mapping[str, object],
    *,
    writer: CandidateWriter | None = None,
    runner: ExperimentRunner | None = None,
) -> dict[str, object]:
    """Apply one structured proposal to an isolated candidate and gate promotion."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("project root must exist")
    if not isinstance(hypothesis, Mapping):
        raise ValueError("hypothesis must be a mapping")

    writer = writer or StructuredHypothesisWriter()
    edits = writer.propose_text_edits(hypothesis)
    if not isinstance(edits, Mapping):
        raise CandidateEditError("candidate writer must return a mapping")
    raw_writer_report = getattr(writer, "last_report", None)
    writer_report = dict(raw_writer_report) if isinstance(raw_writer_report, Mapping) else None
    if not edits:
        return {
            "schema_version": 1,
            "kind": "autonomous_promotion_attempt",
            "policy_version": AUTONOMOUS_PROMOTION_POLICY_VERSION,
            "created_at": utc_now(),
            "status": "no_edit_proposed",
            "outcome": "no_edit_proposed",
            "promotion_performed": False,
            "main_tree_modified": False,
            "writer_report": writer_report,
            "evaluator2": None,
            "evaluator1": None,
            "promotion": None,
        }

    store = ImprovementStore(root)
    attempt_id = "ap_" + uuid4().hex
    workspace = store.workspaces / attempt_id
    candidate_root = workspace / "candidate"
    workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
    manifest = prepare_candidate_workspace(root, candidate_root)
    edit_report = ValidatedCandidateEditor().apply_text_edits(candidate_root, edits)
    structural_check = _structural_goal_check(root, candidate_root, hypothesis)
    if structural_check is not None and structural_check.get("decision") != "pass":
        result = {
            "schema_version": 1,
            "kind": "autonomous_promotion_attempt",
            "policy_version": AUTONOMOUS_PROMOTION_POLICY_VERSION,
            "attempt_id": attempt_id,
            "created_at": utc_now(),
            "workspace": str(workspace),
            "candidate_root": str(candidate_root),
            "hypothesis_id": hypothesis.get("hypothesis_id"),
            "memory_id": hypothesis.get("memory_id"),
            "benchmark_suite": hypothesis.get("benchmark_suite"),
            "candidate_manifest": manifest,
            "edit_report": edit_report,
            "writer_report": writer_report,
            "structural_check": structural_check,
            "baseline": None,
            "candidate": None,
            "evaluator2": None,
            "evaluator1": None,
            "promotion": None,
            "status": "rejected_structural_goal",
            "outcome": str(structural_check.get("decision_code") or "structural_goal_not_met"),
            "promotion_performed": False,
            "main_tree_modified": False,
        }
        _persist(workspace, result)
        return result

    runner = runner or SubprocessExperimentRunner()
    benchmark_selection = select_benchmark_suite(hypothesis, root=root)
    suite = benchmark_selection.get("suite")
    suite = suite if isinstance(suite, str) and suite else None
    baseline = runner.evaluate(root, suite)
    candidate = runner.evaluate(candidate_root, suite)
    evaluator2 = evaluate_candidate(root, candidate_root, baseline, candidate)

    result: dict[str, object] = {
        "schema_version": 1,
        "kind": "autonomous_promotion_attempt",
        "policy_version": AUTONOMOUS_PROMOTION_POLICY_VERSION,
        "attempt_id": attempt_id,
        "created_at": utc_now(),
        "workspace": str(workspace),
        "candidate_root": str(candidate_root),
        "hypothesis_id": hypothesis.get("hypothesis_id"),
        "memory_id": hypothesis.get("memory_id"),
        "benchmark_suite": suite,
        "auto_benchmark": benchmark_selection,
        "candidate_manifest": manifest,
        "edit_report": edit_report,
        "writer_report": writer_report,
        "structural_check": structural_check,
        "baseline": baseline,
        "candidate": candidate,
        "evaluator2": evaluator2,
        "evaluator1": None,
        "promotion": None,
        "status": "rejected",
        "outcome": "rejected_evaluator2",
        "promotion_performed": False,
        "main_tree_modified": False,
    }

    if evaluator2.get("decision") != "accept" or evaluator2.get("decision_code") != "candidate_verified":
        result["status"] = "no_effect_change" if evaluator2.get("decision_code") == "no_change_verified" else "rejected"
        result["outcome"] = str(evaluator2.get("decision_code") or "rejected_evaluator2")
        _persist(workspace, result)
        return result

    evaluator1 = evaluate_final_gate(root, candidate_root, evaluator2, baseline, candidate)
    result["evaluator1"] = evaluator1
    if evaluator1.get("decision") != "allow":
        result["status"] = "denied"
        result["outcome"] = str(evaluator1.get("decision_code") or "evaluator1_denied")
        _persist(workspace, result)
        return result

    promotion = promote_candidate(
        root,
        candidate_root,
        evaluator1,
        evaluator2,
        lambda main_root: runner.evaluate(main_root, suite),
    )
    result["promotion"] = promotion
    result["status"] = str(promotion.get("status") or "promotion_failed")
    result["outcome"] = str(promotion.get("decision_code") or result["status"])
    result["promotion_performed"] = bool(promotion.get("promotion_performed"))
    result["main_tree_modified"] = bool(promotion.get("promotion_performed"))
    _persist(workspace, result)
    return result
