from __future__ import annotations
from collections import Counter
import hashlib
from pathlib import Path
import re
from typing import Any, Mapping

DIAGNOSTIC_POLICY_VERSION = 1
MAX_INPUT_BYTES = 256 * 1024
MAX_DIAGNOSTICS = 40
MAX_MESSAGE_CHARS = 280
MAX_PATH_CHARS = 240

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SECRET = re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization)\b\s*[:=]\s*([^\s,;]+)")
_HOME_PATH = re.compile(r"(?:/[A-Za-z0-9._-]+){2,}")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\s:]+")
_NUMBER = re.compile(r"\b\d+\b")
_QUOTED = re.compile(r"(['\"])[^'\"]{1,120}\1")
_WS = re.compile(r"\s+")

_PATTERNS = {
    "ts": re.compile(r"^(?P<path>.+?\.(?:ts|tsx|js|jsx))\((?P<line>\d+),(?P<col>\d+)\):\s+(?P<severity>error|warning)\s+(?P<code>TS\d+):\s*(?P<msg>.+)$", re.I),
    "gcc": re.compile(r"^(?P<path>.+?\.(?:c|cc|cpp|cxx|h|hh|hpp)):(?P<line>\d+):(?P<col>\d+):\s*(?P<severity>fatal error|error|warning|note):\s*(?P<msg>.+?)(?:\s+\[(?P<code>[^\]]+)\])?$", re.I),
    "go": re.compile(r"^(?P<path>(?:\./)?[^\s:]+\.go):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+)$"),
    "java_maven": re.compile(r"^(?:\[ERROR\]\s*)?(?P<path>.+?\.java):\[(?P<line>\d+),(?P<col>\d+)\]\s*(?P<msg>.+)$"),
    "java": re.compile(r"^(?P<path>.+?\.java):(?P<line>\d+):\s*(?P<severity>error|warning):\s*(?P<msg>.+)$", re.I),
    "dart1": re.compile(r"^(?P<severity>error|warning|info)\s*[•-]\s*(?P<msg>.+?)\s*[•-]\s*(?P<path>.+?\.dart):(?P<line>\d+):(?P<col>\d+)(?:\s*[•-]\s*(?P<code>[A-Za-z0-9_.-]+))?$", re.I),
    "dart2": re.compile(r"^(?P<severity>error|warning|info)\s*-\s*(?P<path>.+?\.dart):(?P<line>\d+):(?P<col>\d+)\s*-\s*(?P<msg>.+?)(?:\s*-\s*(?P<code>[A-Za-z0-9_.-]+))?$", re.I),
    "py_direct": re.compile(r"^(?P<path>.+?\.py):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?P<code>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)|AssertionError)?\s*:?\s*(?P<msg>.*)$"),
    "py_frame": re.compile(r'^\s*File "([^"]+\.py)", line (\d+)'),
    "py_exc": re.compile(r"^(?P<code>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)|AssertionError):\s*(?P<msg>.*)$"),
    "rust_head": re.compile(r"^(?P<severity>error|warning)(?:\[(?P<code>[A-Z]\d+)\])?:\s*(?P<msg>.+)$", re.I),
    "rust_loc": re.compile(r"^\s*-->\s*(?P<path>.+?\.rs):(?P<line>\d+):(?P<col>\d+)"),
    "sql": re.compile(r"^\s*L:\s*(?P<line>\d+)\s*\|\s*P:\s*(?P<col>\d+)\s*\|\s*(?P<code>[A-Z]+\d+)\s*\|\s*(?P<msg>.+?)(?:\s*\[[^\]]+\])?\s*$"),
    "eslint": re.compile(r"^\s*(?P<line>\d+):(?P<col>\d+)\s+(?P<severity>error|warning)\s+(?P<msg>.+?)\s+(?P<code>[@A-Za-z0-9_./-]+)\s*$", re.I),
}

def _sanitize_message(message: str) -> str:
    value = _ANSI.sub("", str(message))
    value = _SECRET.sub(lambda m: f"{m.group(1)}=<redacted>", value)
    value = _WINDOWS_PATH.sub("<path>", value)
    value = _HOME_PATH.sub("<path>", value)
    return _WS.sub(" ", value).strip()[:MAX_MESSAGE_CHARS]

def _message_key(message: str) -> str:
    value = _sanitize_message(message).casefold()
    value = _QUOTED.sub("<value>", value)
    value = _NUMBER.sub("<n>", value)
    return _WS.sub(" ", value).strip()[:MAX_MESSAGE_CHARS]

def _normalize_path(value: str | None, root: Path | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().strip('"').strip("'").replace("\\", "/")
    if len(raw) > 1024:
        return None
    path = Path(raw)
    if path.is_absolute():
        if root is None:
            return None
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return None
    else:
        rel = Path(raw.lstrip("./")).as_posix()
    if not rel or rel.startswith("/") or ".." in Path(rel).parts or len(rel) > MAX_PATH_CHARS:
        return None
    return rel

def _int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 10_000_000 else None

def _severity(value: str | None) -> str:
    value = (value or "error").casefold()
    if "error" in value or "fatal" in value:
        return "error"
    if value == "warning":
        return "warning"
    return "info"

def _row(command_id, language, tool, severity, message, root, *, path=None, line=None, column=None, code=None):
    clean_path = _normalize_path(path, root)
    clean_message = _sanitize_message(message or code or "diagnostic")
    clean_code = str(code).strip()[:80] if code else None
    material = "\n".join([
        command_id, language, clean_path or "", (clean_code or "").casefold(), _message_key(clean_message)
    ]).encode("utf-8")
    return {
        "fingerprint": hashlib.sha256(material).hexdigest(),
        "tool": tool,
        "command_id": command_id,
        "language": language,
        "severity": _severity(severity),
        "code": clean_code,
        "path": clean_path,
        "line": _int(line),
        "column": _int(column),
        "message": clean_message,
    }

def parse_diagnostics(command_id: str, language: str, output: str, *, root: Path | None = None) -> dict[str, Any]:
    data = (output if isinstance(output, str) else "").encode("utf-8", errors="replace")[:MAX_INPUT_BYTES]
    lines = data.decode("utf-8", errors="replace").splitlines()
    diagnostics, seen = [], set()
    tool = command_id.split(".", 1)[0]
    eslint_path = None
    py_frame = None
    rust_head = None

    def add(row):
        key = (row["fingerprint"], row["line"], row["column"])
        if key in seen or len(diagnostics) >= MAX_DIAGNOSTICS:
            return
        seen.add(key)
        diagnostics.append(row)

    for raw in lines:
        line_text = _ANSI.sub("", raw).rstrip()
        if not line_text:
            continue

        matched = False
        for key in ("ts", "gcc", "go", "java_maven", "java", "dart1", "dart2", "sql"):
            m = _PATTERNS[key].match(line_text)
            if not m:
                continue
            g = m.groupdict()
            add(_row(command_id, language, tool, g.get("severity") or ("warning" if key=="sql" else "error"),
                     g.get("msg") or "diagnostic", root, path=g.get("path"), line=g.get("line"),
                     column=g.get("col"), code=g.get("code")))
            matched = True
            break
        if matched:
            continue

        if re.fullmatch(r"\s*(?:\.{0,2}/)?[^\s:]+\.(?:js|jsx|ts|tsx)\s*", line_text):
            eslint_path = line_text.strip()
            continue
        m = _PATTERNS["eslint"].match(line_text)
        if m and eslint_path:
            g = m.groupdict()
            add(_row(command_id, language, tool, g["severity"], g["msg"], root,
                     path=eslint_path, line=g["line"], column=g["col"], code=g["code"]))
            continue

        m = _PATTERNS["py_frame"].match(line_text)
        if m:
            py_frame = (m.group(1), m.group(2))
            continue
        m = _PATTERNS["py_exc"].match(line_text)
        if m and py_frame:
            add(_row(command_id, language, tool, "error", m["msg"], root,
                     path=py_frame[0], line=py_frame[1], code=m["code"]))
            continue
        m = _PATTERNS["py_direct"].match(line_text)
        if m and (m["code"] or "AssertionError" in line_text):
            add(_row(command_id, language, tool, "error", m["msg"] or m["code"] or "python diagnostic", root,
                     path=m["path"], line=m["line"], column=m["col"], code=m["code"]))
            continue

        m = _PATTERNS["rust_head"].match(line_text)
        if m:
            rust_head = (m["severity"], m["code"], m["msg"])
            continue
        m = _PATTERNS["rust_loc"].match(line_text)
        if m and rust_head:
            add(_row(command_id, language, tool, rust_head[0], rust_head[2], root,
                     path=m["path"], line=m["line"], column=m["col"], code=rust_head[1]))
            rust_head = None

    counts = Counter(row["severity"] for row in diagnostics)
    return {
        "schema": "sira.engineering_diagnostics.v1",
        "policy_version": DIAGNOSTIC_POLICY_VERSION,
        "command_id": command_id,
        "language": language,
        "diagnostic_count": len(diagnostics),
        "severity_counts": {"error": counts["error"], "warning": counts["warning"], "info": counts["info"]},
        "diagnostics": diagnostics,
        "truncated": len(diagnostics) >= MAX_DIAGNOSTICS,
        "raw_output_included": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }

def diagnostic_advisory_context(result: Mapping[str, Any]) -> dict[str, Any]:
    rows, counts = [], Counter()
    commands = result.get("commands") if isinstance(result, Mapping) else []
    for command in commands if isinstance(commands, list) else []:
        if not isinstance(command, Mapping):
            continue
        for row in command.get("diagnostics", []) if isinstance(command.get("diagnostics"), list) else []:
            if not isinstance(row, Mapping) or not isinstance(row.get("fingerprint"), str):
                continue
            clean = {k: row.get(k) for k in (
                "fingerprint","tool","command_id","language","severity","code","path","line","column","message"
            )}
            rows.append(clean)
            counts[row["fingerprint"]] += 1
            if len(rows) >= MAX_DIAGNOSTICS:
                break
    return {
        "schema": "sira.engineering_diagnostic_advisory.v1",
        "diagnostics": rows,
        "diagnostic_count": len(rows),
        "fingerprint_counts": dict(sorted(counts.items())),
        "repair_authority": "advisory_only",
        "raw_output_included": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }
