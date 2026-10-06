"""Durable, bounded backoff for free learning-goal search providers."""
from __future__ import annotations

import json
import math
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from .storage import write_json


_COOLDOWN_SECONDS = {
    "http_429": 6 * 60 * 60,
    "http_403": 6 * 60 * 60,
    "http_502": 5 * 60,
    "http_503": 5 * 60,
    "http_504": 5 * 60,
    "network_or_timeout": 5 * 60,
}
_MAX_STATE_BYTES = 64 * 1024
_MAX_RETRY_SECONDS = 24 * 60 * 60


class LearningGoalProviderCooldown:
    def __init__(self, root: Path):
        self.directory = Path(root).resolve() / ".cache"
        self.path = self.directory / "learning_goal_provider_cooldowns.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.directory.is_symlink() or self.path.is_symlink():
            raise ValueError("Learning provider cooldown path is unsafe")
        if not self.path.exists():
            return {}
        if not self.path.is_file() or self.path.stat().st_size > _MAX_STATE_BYTES:
            raise ValueError("Learning provider cooldown state is unsafe")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Learning provider cooldown state is corrupt") from exc
        entries = value.get("providers") if isinstance(value, dict) and value.get("schema_version") == 1 else None
        if not isinstance(entries, dict) or len(entries) > 20:
            raise ValueError("Learning provider cooldown state is invalid")
        for name, row in entries.items():
            until = row.get("until_epoch") if isinstance(row, dict) else None
            if (not isinstance(name, str) or not 1 <= len(name) <= 64
                    or not isinstance(row, dict)
                    or not isinstance(row.get("code"), str)
                    or row["code"] not in _COOLDOWN_SECONDS
                    or type(until) not in (int, float)
                    or not math.isfinite(until) or until < 0):
                raise ValueError("Learning provider cooldown entry is invalid")
        return entries

    def filter(self, providers: Sequence[Any], *, now_epoch: float | None = None) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
        when = time.time() if now_epoch is None else now_epoch
        entries = self._load()
        selected, skipped = [], []
        for provider in providers:
            name = provider.name
            row = entries.get(name)
            remaining = row["until_epoch"] - when if row is not None else 0
            if remaining > 0:
                skipped.append({"provider": name, "code": row["code"],
                                "remaining_seconds": max(1, math.ceil(remaining))})
            else:
                selected.append(provider)
        return tuple(selected), skipped

    def observe(self, report: Mapping[str, Any], *, now_epoch: float | None = None) -> None:
        """Record only fresh requests; cached failures cannot extend backoff."""
        metrics = report.get("metrics")
        if (not isinstance(metrics, Mapping) or metrics.get("cache_hit") is not False
                or type(metrics.get("api_requests")) is not int or metrics["api_requests"] <= 0):
            return
        attempted = report.get("providers_attempted")
        failures = report.get("provider_failures")
        if not isinstance(attempted, list) or not isinstance(failures, list):
            return
        when = time.time() if now_epoch is None else now_epoch
        entries = self._load()
        updated = dict(entries)
        failure_by_name = {
            row.get("provider"): row
            for row in failures if isinstance(row, Mapping)
            and isinstance(row.get("provider"), str)
        }
        for name in attempted:
            if not isinstance(name, str) or not 1 <= len(name) <= 64:
                continue
            failure = failure_by_name.get(name)
            code = failure.get("code") if failure else None
            if code is not None and not isinstance(code, str):
                raise ValueError("Learning provider failure code is invalid")
            if code in _COOLDOWN_SECONDS:
                delay = failure.get("retry_after_seconds")
                delay = (delay if type(delay) is int and 0 < delay <= _MAX_RETRY_SECONDS
                         else _COOLDOWN_SECONDS[code])
                updated[name] = {"code": code, "until_epoch": when + delay}
            else:
                updated.pop(name, None)
        if updated != entries:
            write_json(self.path, {"schema_version": 1, "providers": updated})
