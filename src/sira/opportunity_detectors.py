"""Local deterministic opportunity detectors used by the v1.1 scanner.

Detectors only inspect bounded project text/AST state. They do not call network
providers or models and they do not mutate source files.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any, Iterable


FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def branch_points(node: ast.AST) -> int:
    score = 0
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.Match, ast.comprehension)):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += max(1, len(child.values) - 1)
    return score


def collect_test_identifiers(root: Path, *, max_files: int = 500, max_file_bytes: int = 512 * 1024) -> set[str]:
    """Collect identifiers mentioned by tests.

    This intentionally errs on the conservative side: any matching identifier
    suppresses a missing-direct-test signal, reducing false positives.
    """
    tests_root = Path(root) / "tests"
    identifiers: set[str] = set()
    if not tests_root.is_dir():
        return identifiers
    for path in sorted(tests_root.rglob("*.py"))[:max_files]:
        if path.is_symlink():
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        except (OSError, UnicodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    identifiers.add(alias.asname or alias.name.rsplit(".", 1)[-1])
                    identifiers.add(alias.name.rsplit(".", 1)[-1])
    return identifiers


def _top_level_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [node for node in tree.body if isinstance(node, FUNCTION_NODES)]


def _loc(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    return max(1, int(getattr(node, "end_lineno", node.lineno)) - int(node.lineno) + 1)


def _is_public_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return bool(node.name) and not node.name.startswith("_")


def _weak_handler(handler: ast.ExceptHandler) -> bool:
    broad = handler.type is None
    if isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}:
        broad = True
    if not broad or not handler.body:
        return False
    # Only flag clearly swallowed broad exceptions. Return-based fallbacks may
    # be deliberate contracts and are therefore not treated as weak here.
    return all(isinstance(stmt, (ast.Pass, ast.Continue, ast.Break)) for stmt in handler.body)


def weak_exception_handler_count(node: ast.AST) -> int:
    return sum(1 for child in ast.walk(node) if isinstance(child, ast.ExceptHandler) and _weak_handler(child))


def _normalized_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    module = ast.Module(body=body, type_ignores=[])
    return ast.dump(module, annotate_fields=True, include_attributes=False)


def duplicate_body_groups(tree: ast.Module) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for node in _top_level_functions(tree):
        if len(node.body) < 3 or _loc(node) < 5:
            continue
        digest = hashlib.sha256(_normalized_body(node).encode("utf-8")).hexdigest()
        groups.setdefault(digest, []).append(node.name)
    return {digest: names for digest, names in groups.items() if len(names) >= 2}


def detect_function_signals(tree: ast.Module, rel: str, test_identifiers: set[str]) -> list[dict[str, Any]]:
    """Return normalized local signals for one parsed Python module."""
    signals: list[dict[str, Any]] = []

    # Preserve the v1.0 complexity semantics, including nested functions.
    for node in ast.walk(tree):
        if not isinstance(node, FUNCTION_NODES):
            continue
        loc = _loc(node)
        branches = branch_points(node)
        if loc >= 40 and branches >= 12:
            evidence = {
                "line": int(node.lineno),
                "end_line": int(getattr(node, "end_lineno", node.lineno)),
                "loc": loc,
                "branch_points": branches,
                "threshold_branch_points": 12,
            }
            score = 130 + min(branches, 50) * 5 + min(max(loc - 40, 0), 200)
            signals.append({
                "kind": "complex_function",
                "symbol": node.name,
                "summary": f"{rel}:{node.name} has {branches} branch points across {loc} lines.",
                "evidence": evidence,
                "priority_score": score,
            })

    top_level = _top_level_functions(tree)
    duplicate_groups = duplicate_body_groups(tree)
    duplicate_by_symbol: dict[str, list[str]] = {}
    for names in duplicate_groups.values():
        for name in names:
            duplicate_by_symbol[name] = [peer for peer in names if peer != name]

    for node in top_level:
        loc = _loc(node)
        branches = branch_points(node)

        if _is_public_function(node) and node.name not in test_identifiers and (loc >= 12 or branches >= 3):
            signals.append({
                "kind": "missing_test_reference",
                "symbol": node.name,
                "summary": f"{rel}:{node.name} has no direct test reference despite {loc} lines and {branches} branch points.",
                "evidence": {
                    "line": int(node.lineno),
                    "end_line": int(getattr(node, "end_lineno", node.lineno)),
                    "loc": loc,
                    "branch_points": branches,
                    "test_reference_count": 0,
                    "minimum_loc": 12,
                    "minimum_branch_points": 3,
                },
                "priority_score": 110 + min(branches, 12) * 5 + min(loc, 50),
            })

        weak_count = weak_exception_handler_count(node)
        if weak_count:
            signals.append({
                "kind": "weak_error_handling",
                "symbol": node.name,
                "summary": f"{rel}:{node.name} silently swallows {weak_count} broad exception handler(s).",
                "evidence": {
                    "line": int(node.lineno),
                    "end_line": int(getattr(node, "end_lineno", node.lineno)),
                    "loc": loc,
                    "weak_exception_handlers": weak_count,
                },
                "priority_score": 220 + min(weak_count, 5) * 20 + min(branches, 10) * 3,
            })

        peers = duplicate_by_symbol.get(node.name, [])
        if peers:
            signals.append({
                "kind": "duplicate_function_body",
                "symbol": node.name,
                "summary": f"{rel}:{node.name} duplicates the normalized body of {len(peers)} peer function(s).",
                "evidence": {
                    "line": int(node.lineno),
                    "end_line": int(getattr(node, "end_lineno", node.lineno)),
                    "loc": loc,
                    "duplicate_body_matches": len(peers),
                    "peer_symbols": peers[:10],
                },
                "priority_score": 190 + min(len(peers), 5) * 20 + min(loc, 40),
            })

    return signals
