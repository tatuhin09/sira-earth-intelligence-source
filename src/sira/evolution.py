"""Bounded strategy evolution planning for autonomous improvement.

v1.4A is deliberately advisory/planning-only. It tracks aggregate strategy
outcomes per stable opportunity lineage, prevents exact strategy retry loops,
deduplicates equivalent strategy text, and suppresses immediate re-evolution
after a successful promotion. It does not edit code, evaluate candidates, or
authorize promotion.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping

from .autonomous_targeting import opportunity_lineage_key
from .models import utc_now
from .storage import write_json

EVOLUTION_POLICY_VERSION = 1
HISTORY_SCHEMA_VERSION = 1
SUCCESS_SUPPRESSION_SECONDS = 24 * 60 * 60
MAX_LINEAGES = 1000
MAX_ATTEMPTS_PER_LINEAGE = 100
MAX_STRATEGIES = 32
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


class EvolutionError(ValueError):
    pass


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _normalized_strategy_text(value: object) -> str:
    if not isinstance(value, str):
        raise EvolutionError("strategy text must be a string")
    normalized = " ".join(value.casefold().split())
    if not normalized or len(normalized) > 4000:
        raise EvolutionError("strategy text must contain 1..4000 normalized characters")
    return normalized


def _strategy_fingerprint(strategy: Mapping[str, Any]) -> str:
    text = _normalized_strategy_text(strategy.get("strategy"))
    basis = strategy.get("basis")
    basis = " ".join(str(basis or "").casefold().split())[:500]
    citations = strategy.get("citation_ids")
    citation_ids = sorted({
        str(item).strip()[:120]
        for item in citations
        if isinstance(item, str) and item.strip()
    }) if isinstance(citations, list) else []
    payload = {
        "strategy": text,
        "basis": basis,
        "citation_ids": citation_ids,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sanitize_strategy(strategy: Mapping[str, Any]) -> dict[str, Any]:
    text = strategy.get("strategy")
    _normalized_strategy_text(text)
    basis = strategy.get("basis")
    citations = strategy.get("citation_ids")
    clean_citations = []
    if isinstance(citations, list):
        for item in citations[:20]:
            if isinstance(item, str) and item.strip():
                clean_citations.append(item.strip()[:120])
    return {
        "strategy": " ".join(str(text).split())[:4000],
        "basis": " ".join(str(basis or "").split())[:500],
        "citation_ids": clean_citations,
    }


class StrategyEvolutionPlanner:
    """Track aggregate strategy history and select the next distinct strategy."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.base = self.root / "improvements" / "evolution"
        self.path = self.base / "strategy_history.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            if self.path.is_symlink() or self.path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise EvolutionError("evolution strategy history is unsafe")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvolutionError("evolution strategy history is corrupt") from exc

        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != HISTORY_SCHEMA_VERSION
            or not isinstance(raw.get("lineages"), dict)
        ):
            raise EvolutionError("evolution strategy history format is invalid")

        lineages = raw["lineages"]
        if len(lineages) > MAX_LINEAGES:
            raise EvolutionError("evolution strategy history is too large")

        for lineage_key, entry in lineages.items():
            if (
                not isinstance(lineage_key, str)
                or len(lineage_key) != 64
                or not isinstance(entry, dict)
                or not isinstance(entry.get("attempts"), list)
                or len(entry["attempts"]) > MAX_ATTEMPTS_PER_LINEAGE
            ):
                raise EvolutionError("evolution strategy history contains invalid entries")
            for attempt in entry["attempts"]:
                if (
                    not isinstance(attempt, dict)
                    or not isinstance(attempt.get("strategy_fingerprint"), str)
                    or len(attempt["strategy_fingerprint"]) != 64
                    or not isinstance(attempt.get("outcome"), str)
                    or not isinstance(attempt.get("promotion_performed"), bool)
                    or type(attempt.get("attempted_at_epoch")) not in (int, float)
                ):
                    raise EvolutionError("evolution strategy history contains invalid attempts")
        return lineages

    def _save(self, lineages: dict[str, dict[str, Any]]) -> None:
        if len(lineages) > MAX_LINEAGES:
            ordered = sorted(
                lineages.items(),
                key=lambda item: max(
                    (
                        float(row.get("attempted_at_epoch", 0.0))
                        for row in item[1].get("attempts", [])
                    ),
                    default=0.0,
                ),
                reverse=True,
            )
            lineages = dict(ordered[:MAX_LINEAGES])
        write_json(
            self.path,
            {
                "schema_version": HISTORY_SCHEMA_VERSION,
                "policy_version": EVOLUTION_POLICY_VERSION,
                "updated_at": utc_now(),
                "lineages": lineages,
            },
        )

    @staticmethod
    def _deduplicate(
        strategies: list[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        if not isinstance(strategies, list) or not strategies:
            raise EvolutionError("candidate strategies must be a non-empty list")
        if len(strategies) > MAX_STRATEGIES:
            raise EvolutionError("too many candidate strategies")

        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        duplicates = 0
        for row in strategies:
            if not isinstance(row, Mapping):
                raise EvolutionError("candidate strategy must be an object")
            clean = _sanitize_strategy(row)
            fingerprint = _strategy_fingerprint(clean)
            if fingerprint in seen:
                duplicates += 1
                continue
            seen.add(fingerprint)
            unique.append({**clean, "strategy_fingerprint": fingerprint})
        if not unique:
            raise EvolutionError("candidate strategies are empty after normalization")
        return unique, duplicates

    @staticmethod
    def _history_summary(attempts: list[dict[str, Any]]) -> dict[str, int]:
        promoted = sum(
            1
            for row in attempts
            if row.get("promotion_performed") is True
            or row.get("outcome") == "promotion_committed"
        )
        attempted = len(attempts)
        return {
            "attempted": attempted,
            "rejected_or_failed": attempted - promoted,
            "promoted": promoted,
            "distinct_strategies": len({
                str(row.get("strategy_fingerprint"))
                for row in attempts
                if isinstance(row.get("strategy_fingerprint"), str)
            }),
        }

    def plan(
        self,
        opportunity: dict[str, Any],
        strategies: list[Mapping[str, Any]],
        *,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        when = time.time() if now_epoch is None else now_epoch
        if type(when) not in (int, float) or when < 0:
            raise EvolutionError("now_epoch must be a non-negative number")

        lineage_key = opportunity_lineage_key(opportunity)
        unique, duplicate_count = self._deduplicate(strategies)
        lineages = self._load()
        entry = lineages.get(lineage_key, {"attempts": []})
        attempts = list(entry.get("attempts", []))
        summary = self._history_summary(attempts)
        history_applied = bool(attempts)

        promoted_attempts = [
            row
            for row in attempts
            if (
                row.get("promotion_performed") is True
                or row.get("outcome") == "promotion_committed"
            )
            and float(row.get("attempted_at_epoch", 0.0)) <= float(when)
        ]
        latest_success = max(
            promoted_attempts,
            key=lambda row: float(row.get("attempted_at_epoch", 0.0)),
            default=None,
        )
        if latest_success is not None:
            elapsed = max(
                0.0,
                float(when) - float(latest_success["attempted_at_epoch"]),
            )
            if elapsed < SUCCESS_SUPPRESSION_SECONDS:
                return {
                    "schema": "sira.evolution_strategy_plan.v1",
                    "policy_version": EVOLUTION_POLICY_VERSION,
                    "status": "recent_success_suppressed",
                    "lineage_key": lineage_key,
                    "candidate_strategy_count": len(unique),
                    "deduplicated_strategy_count": duplicate_count,
                    "attempted_strategy_count": summary["distinct_strategies"],
                    "history_applied": True,
                    "history_summary": summary,
                    "selected_index": None,
                    "selected_strategy": None,
                    "selected_strategy_fingerprint": None,
                    "remaining_success_suppression_seconds": max(
                        1, int(SUCCESS_SUPPRESSION_SECONDS - elapsed)
                    ),
                }

        attempted_fingerprints = {
            str(row.get("strategy_fingerprint"))
            for row in attempts
            if isinstance(row.get("strategy_fingerprint"), str)
        }

        selected_index = None
        selected = None
        for index, strategy in enumerate(unique):
            if strategy["strategy_fingerprint"] not in attempted_fingerprints:
                selected_index = index
                selected = strategy
                break

        if selected is None:
            return {
                "schema": "sira.evolution_strategy_plan.v1",
                "policy_version": EVOLUTION_POLICY_VERSION,
                "status": "strategy_exhausted",
                "lineage_key": lineage_key,
                "candidate_strategy_count": len(unique),
                "deduplicated_strategy_count": duplicate_count,
                "attempted_strategy_count": summary["distinct_strategies"],
                "history_applied": history_applied,
                "history_summary": summary,
                "selected_index": None,
                "selected_strategy": None,
                "selected_strategy_fingerprint": None,
                "remaining_success_suppression_seconds": 0,
            }

        clean_selected = {
            "strategy": selected["strategy"],
            "basis": selected["basis"],
            "citation_ids": list(selected["citation_ids"]),
        }
        return {
            "schema": "sira.evolution_strategy_plan.v1",
            "policy_version": EVOLUTION_POLICY_VERSION,
            "status": "selected",
            "lineage_key": lineage_key,
            "candidate_strategy_count": len(unique),
            "deduplicated_strategy_count": duplicate_count,
            "attempted_strategy_count": summary["distinct_strategies"],
            "history_applied": history_applied,
            "history_summary": summary,
            "selected_index": selected_index,
            "selected_strategy": clean_selected,
            "selected_strategy_fingerprint": selected["strategy_fingerprint"],
            "remaining_success_suppression_seconds": 0,
        }

    def record_outcome(
        self,
        opportunity: dict[str, Any],
        strategy: Mapping[str, Any],
        *,
        outcome: str,
        promotion_performed: bool,
        attempted_at_epoch: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(outcome, str) or not 1 <= len(outcome.strip()) <= 120:
            raise EvolutionError("outcome must be 1..120 characters")
        if not isinstance(promotion_performed, bool):
            raise EvolutionError("promotion_performed must be boolean")
        when = time.time() if attempted_at_epoch is None else attempted_at_epoch
        if type(when) not in (int, float) or when < 0:
            raise EvolutionError("attempted_at_epoch must be a non-negative number")

        lineage_key = opportunity_lineage_key(opportunity)
        strategy_fp = _strategy_fingerprint(strategy)
        lineages = self._load()
        entry = lineages.setdefault(
            lineage_key,
            {
                "type": opportunity.get("type"),
                "path": opportunity.get("path"),
                "symbol": opportunity.get("symbol"),
                "attempts": [],
            },
        )
        event = {
            "strategy_fingerprint": strategy_fp,
            "outcome": outcome.strip(),
            "promotion_performed": promotion_performed,
            "attempted_at_epoch": float(when),
            "attempted_at": _iso_from_epoch(float(when)),
        }
        entry["attempts"].append(event)
        entry["attempts"].sort(
            key=lambda row: float(row["attempted_at_epoch"]),
            reverse=True,
        )
        del entry["attempts"][MAX_ATTEMPTS_PER_LINEAGE:]
        self._save(lineages)

        return {
            "schema": "sira.evolution_strategy_outcome.v1",
            "lineage_key": lineage_key,
            "strategy_fingerprint": strategy_fp,
            "outcome": outcome.strip(),
            "promotion_performed": promotion_performed,
            "attempted_at": event["attempted_at"],
        }
