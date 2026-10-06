"""Per-host fetch health for learning-goal documents.

A trial showed ``doaj.org`` answering HTTP 403 to every article request (5 of 16 read
failures), yet DOAJ kept winning the limited source slots so readable pages were never
tried. This remembers hosts that repeatedly refuse automated reads (401/403) and hides
their rows from discovery for a while. It never tries to evade a refusal: the host is
simply skipped. A successful read clears the record; a block expires on its own.

State: ``memory/source_fetch_health/health.json`` (bounded, atomic). A malformed file
blocks nothing. No network, no credentials, no authority.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

SCHEMA = "sira.source_fetch_health.v1"
BLOCK_CODES = frozenset({"http_401", "http_403"})
BLOCK_AFTER_FAILURES = 3
BLOCK_SECONDS = 7 * 86_400
MAX_HOSTS = 100


def _path(root: Path) -> Path:
    return Path(root) / "memory" / "source_fetch_health" / "health.json"


def _load(root: Path) -> dict:
    path = _path(root)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return {}
        hosts = data.get("hosts")
        if not isinstance(hosts, dict):
            return {}
        clean = {}
        for host, row in hosts.items():
            if (isinstance(host, str) and isinstance(row, dict)
                    and type(row.get("refusals")) is int and row["refusals"] >= 0
                    and isinstance(row.get("last_epoch"), (int, float))):
                clean[host] = {"refusals": row["refusals"], "last_epoch": float(row["last_epoch"])}
        return clean
    except (OSError, ValueError, UnicodeError, TypeError):
        return {}


def _save(root: Path, hosts: Mapping[str, Mapping]) -> None:
    path = _path(root)
    directory = path.parent
    if directory.is_symlink() or Path(root, "memory").is_symlink():
        return
    ordered = sorted(hosts.items(), key=lambda item: item[1]["last_epoch"], reverse=True)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".health.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps({"schema": SCHEMA, "hosts": dict(ordered[:MAX_HOSTS])}),
                         encoding="utf-8")
    os.replace(temporary, path)


def observe_fetches(root: Path, *, ok_hosts: Iterable[str] = (),
                    failures: Iterable[Mapping[str, Any]] = (), now_epoch: float) -> None:
    """Record read outcomes. Never raises: health is advisory."""
    try:
        hosts = _load(root)
        for host in {h for h in ok_hosts if isinstance(h, str)}:
            hosts.pop(host, None)
        for failure in failures:
            if not isinstance(failure, Mapping):
                continue
            host, code = failure.get("host"), failure.get("code")
            if not isinstance(host, str) or not host or host in ok_hosts:
                continue
            if code in BLOCK_CODES:
                row = hosts.get(host, {"refusals": 0, "last_epoch": 0.0})
                hosts[host] = {"refusals": row["refusals"] + 1, "last_epoch": float(now_epoch)}
        _save(root, hosts)
    except Exception:  # noqa: BLE001 - advisory bookkeeping must not break learning
        return


def blocked_hosts(root: Path, now_epoch: float) -> frozenset[str]:
    try:
        return frozenset(
            host for host, row in _load(root).items()
            if row["refusals"] >= BLOCK_AFTER_FAILURES
            and 0 <= now_epoch - row["last_epoch"] < BLOCK_SECONDS)
    except Exception:  # noqa: BLE001
        return frozenset()


def drop_blocked_rows(root: Path, report: Mapping[str, Any], now_epoch: float) -> dict:
    """Remove result rows from refusing hosts; record how many were skipped."""
    blocked = blocked_hosts(root, now_epoch)
    result = dict(report)
    rows = report.get("results")
    if not blocked or not isinstance(rows, list):
        return result
    kept, skipped = [], 0
    for row in rows:
        host = urlsplit(str(row.get("url"))).hostname if isinstance(row, Mapping) else None
        if host in blocked:
            skipped += 1
        else:
            kept.append(row)
    result["results"] = kept
    metrics = dict(report.get("metrics") or {})
    metrics["result_count"] = len(kept)
    result["metrics"] = metrics
    result["rows_skipped_refusing_hosts"] = skipped
    result["refusing_hosts"] = sorted(blocked)
    return result
