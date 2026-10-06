"""Fail-closed Python symbol-scoped engineering edit materialization.

Model output may describe one bounded replacement block for an already supplied
Python target symbol. SIRA reconstructs the complete file locally before the
existing candidate editor, verification, evaluator and promotion chain sees it.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import textwrap
from typing import Mapping

MAX_SCOPED_BLOCK_BYTES = 64 * 1024
MAX_SCOPED_DEFINITIONS = 1
MAX_REFERENCED_SIGNATURES = 8
MAX_REFERENCED_SIGNATURE_BYTES = 2048
_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,239}$")
_PRIMARY_SYMBOL_RE = re.compile(
    r"\bPrimary\s+symbol\s*:\s*([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
_PATH_SYMBOL_RE = re.compile(
    r"\bImprove\s+\S+?:([A-Za-z_][A-Za-z0-9_]*)\s+with\b",
    re.IGNORECASE,
)


class EngineeringScopedEditError(ValueError):
    """Raised when a scoped model edit cannot be proven safe to materialize."""


@dataclass(frozen=True, slots=True)
class ScopedPythonSymbolEdit:
    symbol: str
    content: str


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _valid_symbol(value: object) -> str | None:
    if isinstance(value, str) and _SYMBOL_RE.fullmatch(value):
        return value
    return None


def target_symbol_from_task(task: Mapping[str, object]) -> str | None:
    direct = _valid_symbol(task.get("target_symbol"))
    if direct:
        return direct

    target = task.get("target")
    if isinstance(target, Mapping):
        direct = _valid_symbol(target.get("symbol"))
        if direct:
            return direct

    criteria = task.get("success_criteria")
    if isinstance(criteria, Mapping):
        goal = criteria.get("structural_goal")
        if isinstance(goal, Mapping):
            direct = _valid_symbol(goal.get("symbol"))
            if direct:
                return direct

    instruction = task.get("instruction")
    if isinstance(instruction, str):
        for pattern in (_PRIMARY_SYMBOL_RE, _PATH_SYMBOL_RE):
            match = pattern.search(instruction)
            if match:
                return match.group(1)
    return None


def _function_matches(tree: ast.AST, symbol: str):
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == symbol
    ]


def _call_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Describe Python's call syntax without exposing defaults or function bodies."""
    args = node.args
    parts = [item.arg for item in args.posonlyargs]
    if args.posonlyargs:
        parts.append("/")
    parts.extend(item.arg for item in args.args)
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")
    parts.extend(item.arg for item in args.kwonlyargs)
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(parts)})"


def _referenced_local_signatures(source: str, symbol: str) -> list[dict[str, str]]:
    tree = ast.parse(source)
    targets = _function_matches(tree, symbol)
    if len(targets) != 1:
        raise EngineeringScopedEditError("scoped target is ambiguous")
    definitions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.setdefault(node.name, []).append(node)

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    used = 0
    for call in ast.walk(targets[0]):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        name = call.func.id
        if name in seen or name == symbol:
            continue
        seen.add(name)
        matches = definitions.get(name, [])
        if len(matches) != 1 or matches[0].decorator_list:
            continue
        signature = _call_signature(matches[0])
        size = len(signature.encode("utf-8"))
        if size > 256 or used + size > MAX_REFERENCED_SIGNATURE_BYTES:
            continue
        rows.append({"name": name, "signature": signature})
        used += size
        if len(rows) == MAX_REFERENCED_SIGNATURES:
            break
    return rows


def _check_direct_local_helper_calls(tree: ast.Module, target: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    """Reject only calls whose positional overflow is provable from local source."""
    helpers: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            helpers.setdefault(node.name, []).append(node)

    arguments = target.args
    bound = {
        item.arg for item in (
            arguments.posonlyargs + arguments.args + arguments.kwonlyargs
        )
    }
    if arguments.vararg:
        bound.add(arguments.vararg.arg)
    if arguments.kwarg:
        bound.add(arguments.kwarg.arg)
    # A later module assignment or import may replace a function definition;
    # its original signature is no longer authoritative at the call site.
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                bound.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
    # If a name might resolve to a local value instead of a module function,
    # static call-arity validation would be unsound, so leave it to tests.
    for node in ast.walk(target):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not target:
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)

    class DirectCalls(ast.NodeVisitor):
        def visit_Call(self, call: ast.Call) -> None:
            if isinstance(call.func, ast.Name) and call.func.id not in bound:
                matches = helpers.get(call.func.id, [])
                if len(matches) == 1 and not matches[0].decorator_list:
                    args = matches[0].args
                    if (args.vararg is None
                            and not any(isinstance(item, ast.Starred) for item in call.args)
                            and len(call.args) > len(args.posonlyargs) + len(args.args)):
                        raise EngineeringScopedEditError(
                            "scoped edit passes too many positional arguments to a local helper"
                        )
            self.generic_visit(call)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            pass

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            pass

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            pass

        def visit_Lambda(self, node: ast.Lambda) -> None:
            pass

    visitor = DirectCalls()
    for statement in target.body:
        visitor.visit(statement)


def _node_start(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    rows = [int(node.lineno)]
    rows.extend(
        int(item.lineno)
        for item in node.decorator_list
        if getattr(item, "lineno", None) is not None
    )
    return min(rows)


def _node_end(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    value = getattr(node, "end_lineno", None)
    if type(value) is not int:
        raise EngineeringScopedEditError(
            "scoped target has no bounded end line"
        )
    return int(value)


def _signature_fingerprint(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, str, str]:
    kind = type(node).__name__
    args = ast.dump(node.args, include_attributes=False)
    returns = (
        ast.dump(node.returns, include_attributes=False)
        if node.returns is not None
        else ""
    )
    return kind, args, returns


def _decorator_fingerprint(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, ...]:
    return tuple(
        ast.dump(item, include_attributes=False)
        for item in node.decorator_list
    )


def build_python_symbol_scope(
    file_row: Mapping[str, object],
    task: Mapping[str, object],
) -> dict[str, object] | None:
    relative = file_row.get("path")
    content = file_row.get("content")
    digest = file_row.get("sha256")
    editable = file_row.get("editable")
    symbol = target_symbol_from_task(task)

    if (
        not isinstance(relative, str)
        or not relative.endswith(".py")
        or not isinstance(content, str)
        or not isinstance(digest, str)
        or len(digest) != 64
        or editable is not True
        or symbol is None
    ):
        return None

    try:
        tree = ast.parse(content, filename=relative)
    except SyntaxError:
        return None
    matches = _function_matches(tree, symbol)
    if len(matches) != 1:
        return None
    node = matches[0]
    start = _node_start(node)
    end = _node_end(node)
    lines = content.splitlines(keepends=True)
    block = "".join(lines[start - 1:end])
    return {
        "mode": "python_symbol_block",
        "path": relative,
        "symbol": symbol,
        "start_line": start,
        "end_line": end,
        "source_sha256": digest,
        "symbol_sha256": _sha256(block.encode("utf-8")),
        "max_generated_block_bytes": MAX_SCOPED_BLOCK_BYTES,
        "max_definitions": MAX_SCOPED_DEFINITIONS,
        "contract": (
            "Return one edit with this exact path and symbol. Its content must "
            "contain only the complete replacement definition for the target "
            "symbol. Do not add sibling helper definitions. "
            "Do not return the whole file. Do not include imports, classes, "
            "assignments, expressions, or other module/class-level statements."
        ),
    }


def _parse_replacement_block(
    content: str,
    symbol: str,
    original: ast.FunctionDef | ast.AsyncFunctionDef,
    existing_function_names: set[str],
) -> str:
    if not isinstance(content, str) or "\x00" in content:
        raise EngineeringScopedEditError("invalid scoped edit content")
    encoded = content.encode("utf-8")
    if not encoded or len(encoded) > MAX_SCOPED_BLOCK_BYTES:
        raise EngineeringScopedEditError("scoped edit block exceeds limit")

    dedented = textwrap.dedent(content).strip("\n")
    if not dedented:
        raise EngineeringScopedEditError("scoped edit block is empty")
    try:
        tree = ast.parse(dedented + "\n")
    except SyntaxError:
        raise EngineeringScopedEditError(
            "scoped edit block is not valid Python"
        ) from None

    if len(tree.body) != MAX_SCOPED_DEFINITIONS:
        raise EngineeringScopedEditError(
            "scoped edit block must contain target definition only"
        )
    if not all(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    ):
        raise EngineeringScopedEditError(
            "scoped edit block may contain functions only"
        )

    first = tree.body[0]
    assert isinstance(first, (ast.FunctionDef, ast.AsyncFunctionDef))
    if first.name != symbol:
        raise EngineeringScopedEditError(
            "scoped edit must begin with the target symbol"
        )
    if _signature_fingerprint(first) != _signature_fingerprint(original):
        raise EngineeringScopedEditError(
            "scoped target signature must remain unchanged"
        )
    if _decorator_fingerprint(first) != _decorator_fingerprint(original):
        raise EngineeringScopedEditError(
            "scoped target decorators must remain unchanged"
        )

    new_names: set[str] = set()
    for node in tree.body[1:]:
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if not node.name.startswith("_"):
            raise EngineeringScopedEditError(
                "new scoped sibling helpers must be private"
            )
        if node.name in new_names or node.name in existing_function_names:
            raise EngineeringScopedEditError(
                "new scoped helper name collides with existing code"
            )
        if node.decorator_list:
            raise EngineeringScopedEditError(
                "new scoped helpers may not add decorators"
            )
        new_names.add(node.name)

    return dedented


def _materialize_one(
    original_text: str,
    relative: str,
    edit: ScopedPythonSymbolEdit,
) -> str:
    try:
        tree = ast.parse(original_text, filename=relative)
    except SyntaxError:
        raise EngineeringScopedEditError(
            "scoped source is not valid Python"
        ) from None

    matches = _function_matches(tree, edit.symbol)
    if len(matches) != 1:
        raise EngineeringScopedEditError(
            "scoped target symbol is missing or ambiguous"
        )
    original = matches[0]
    start = _node_start(original)
    end = _node_end(original)

    existing_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node is not original
    }
    replacement = _parse_replacement_block(
        edit.content,
        edit.symbol,
        original,
        existing_names,
    )

    lines = original_text.splitlines(keepends=True)
    if not 1 <= start <= end <= len(lines):
        raise EngineeringScopedEditError(
            "scoped target line range is invalid"
        )
    first_line = lines[start - 1]
    indent = first_line[: len(first_line) - len(first_line.lstrip())]
    rendered = textwrap.indent(replacement, indent)
    original_segment = "".join(lines[start - 1:end])
    if original_segment.endswith(("\n", "\r")):
        rendered += "\n"

    candidate = "".join(lines[: start - 1]) + rendered + "".join(lines[end:])
    try:
        candidate_tree = ast.parse(candidate, filename=relative)
    except SyntaxError:
        raise EngineeringScopedEditError(
            "materialized scoped candidate is not valid Python"
        ) from None

    candidate_matches = _function_matches(candidate_tree, edit.symbol)
    if len(candidate_matches) != 1:
        raise EngineeringScopedEditError(
            "materialized scoped target became ambiguous"
        )
    if _signature_fingerprint(candidate_matches[0]) != _signature_fingerprint(original):
        raise EngineeringScopedEditError(
            "materialized scoped signature changed"
        )
    _check_direct_local_helper_calls(candidate_tree, candidate_matches[0])
    return candidate


def materialize_engineering_edits(
    root: Path,
    edits: Mapping[str, object],
    project_context: Mapping[str, object],
    *,
    max_edit_file_bytes: int,
    max_patch_bytes: int,
) -> dict[str, str]:
    root = Path(root).resolve()
    rows = project_context.get("files")
    if not isinstance(rows, list):
        raise EngineeringScopedEditError("engineering context files are invalid")
    supplied = {
        row.get("path"): row
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("path"), str)
    }
    scope = project_context.get("scoped_edit")
    scope = scope if isinstance(scope, Mapping) else None

    result: dict[str, str] = {}
    total = 0
    for relative, value in edits.items():
        if isinstance(value, str):
            materialized = value
        elif isinstance(value, ScopedPythonSymbolEdit):
            if (
                scope is None
                or scope.get("mode") != "python_symbol_block"
                or scope.get("path") != relative
                or scope.get("symbol") != value.symbol
            ):
                raise EngineeringScopedEditError(
                    "scoped model edit is outside the authorized target"
                )
            row = supplied.get(relative)
            if not isinstance(row, Mapping):
                raise EngineeringScopedEditError(
                    "scoped edit requires complete supplied source"
                )
            source_text = row.get("content")
            source_sha = row.get("sha256")
            if not isinstance(source_text, str) or not isinstance(source_sha, str):
                raise EngineeringScopedEditError(
                    "scoped source metadata is invalid"
                )

            path = root / relative
            if path.is_symlink() or not path.is_file():
                raise EngineeringScopedEditError(
                    "scoped source is unavailable"
                )
            try:
                current = path.read_bytes()
            except OSError:
                raise EngineeringScopedEditError(
                    "scoped source cannot be read"
                ) from None
            try:
                current_text = current.decode("utf-8")
            except UnicodeDecodeError:
                raise EngineeringScopedEditError(
                    "scoped source is not UTF-8"
                ) from None
            if (
                _sha256(current) != source_sha
                or scope.get("source_sha256") != source_sha
                or current_text != source_text
            ):
                raise EngineeringScopedEditError(
                    "scoped source changed after context capture"
                )
            materialized = _materialize_one(
                source_text,
                relative,
                value,
            )
        else:
            raise EngineeringScopedEditError(
                "unsupported engineering edit representation"
            )

        payload = materialized.encode("utf-8")
        if len(payload) > max_edit_file_bytes:
            raise EngineeringScopedEditError(
                "materialized engineering file exceeds limit"
            )
        total += len(payload)
        if total > max_patch_bytes:
            raise EngineeringScopedEditError(
                "materialized engineering patch exceeds limit"
            )
        result[relative] = materialized
    return result



def compact_engineering_model_context(project_context: Mapping[str, object]) -> dict[str, object]:
    """Return a model-facing copy containing only the authorized Python symbol block."""
    result = deepcopy(dict(project_context))
    scope = result.get("scoped_edit")
    if not isinstance(scope, Mapping) or scope.get("mode") != "python_symbol_block":
        return result

    relative = scope.get("path")
    start = scope.get("start_line")
    end = scope.get("end_line")
    source_sha = scope.get("source_sha256")
    symbol_sha = scope.get("symbol_sha256")
    if (not isinstance(relative, str) or type(start) is not int or type(end) is not int
            or not 1 <= start <= end or not isinstance(source_sha, str)
            or not isinstance(symbol_sha, str)):
        raise EngineeringScopedEditError("scoped model context metadata is invalid")

    rows = result.get("files")
    if not isinstance(rows, list):
        raise EngineeringScopedEditError("scoped model context files are invalid")

    matched = 0
    for row in rows:
        if not isinstance(row, dict) or row.get("path") != relative:
            continue
        matched += 1
        content = row.get("content")
        if not isinstance(content, str) or row.get("sha256") != source_sha:
            raise EngineeringScopedEditError("scoped model source metadata is inconsistent")
        encoded = content.encode("utf-8")
        if _sha256(encoded) != source_sha:
            raise EngineeringScopedEditError("scoped model source checksum mismatch")
        lines = content.splitlines(keepends=True)
        if end > len(lines):
            raise EngineeringScopedEditError("scoped model line range is invalid")
        block = "".join(lines[start - 1:end])
        block_bytes = block.encode("utf-8")
        if _sha256(block_bytes) != symbol_sha:
            raise EngineeringScopedEditError("scoped model symbol checksum mismatch")
        row["content"] = block
        row["model_context_scope"] = "python_symbol_block"
        row["original_file_bytes"] = len(encoded)
        row["model_content_bytes"] = len(block_bytes)

    if matched != 1:
        raise EngineeringScopedEditError("scoped model target must appear exactly once")

    scope["referenced_signatures"] = _referenced_local_signatures(content, scope["symbol"])

    result["local_context_bytes"] = project_context.get("context_bytes")
    result["context_bytes"] = sum(
        len(row.get("content", "").encode("utf-8"))
        for row in rows if isinstance(row, Mapping) and isinstance(row.get("content"), str)
    )
    result.pop("context_sha256", None)
    canonical = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result["context_sha256"] = _sha256(canonical)
    return result
