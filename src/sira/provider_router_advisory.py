from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

if __package__ in {None, ""}:
    ROOT = Path(__file__).resolve().parents[2]
    SRC = ROOT / "src"
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

from sira.provider_catalog import COST_FREE, list_providers
from sira.provider_health import (
    HEALTH_DEGRADED_HISTORY,
    HEALTH_READY,
    build_provider_health_snapshot,
)
from sira.strategy_learner import StrategyLearner

ADVISORY_SCHEMA = "sira.provider_router_advisory.v1"


def _health_rows(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = snapshot.get("providers")
    if not isinstance(rows, list):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        provider_id = row.get("provider_id")
        if isinstance(provider_id, str) and provider_id:
            result[provider_id] = row
    return result


def _priority(row: Mapping[str, Any]) -> tuple[int, str]:
    health = str(row.get("health") or "")
    cost_class = str(row.get("cost_class") or "")
    free_penalty = 0 if cost_class == COST_FREE else 2
    if health == HEALTH_READY:
        health_penalty = 0
    elif health == HEALTH_DEGRADED_HISTORY:
        health_penalty = 1
    else:
        health_penalty = 9
    return health_penalty + free_penalty, str(row.get("provider_id") or "")


def _apply_strategy_learning(
    root: Path,
    capability: str | None,
    rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], bool]:
    """Reorder only within the existing health/cost priority band."""
    baseline = sorted(rows, key=_priority)
    if not capability:
        return baseline, False

    db = root / "memory" / "sira_memory.sqlite3"
    if not db.is_file():
        return baseline, False

    learner = StrategyLearner(root)
    learned_any = False
    result: list[dict[str, object]] = []

    bands: dict[int, list[dict[str, object]]] = {}
    for row in baseline:
        band = _priority(row)[0]
        bands.setdefault(band, []).append(row)

    for band in sorted(bands):
        group = bands[band]
        decision = learner.rank(
            capability,
            [
                {
                    "strategy_id": str(row["provider_id"]),
                    "base_score": 0.5,
                    "available": True,
                    "policy_allowed": True,
                    "metered": str(row.get("cost_class") or "") != COST_FREE,
                }
                for row in group
            ],
        )
        by_id = {
            str(item["strategy_id"]): item
            for item in decision["ranking"]
        }
        learned_any = learned_any or bool(decision["used_learning"])

        enriched = []
        for row in group:
            item = by_id[str(row["provider_id"])]
            enriched.append({
                **row,
                "learned_adjustment": float(item["learned_adjustment"]),
                "strategy_evidence_events": int(item["evidence"]["eligible_events"]),
            })

        # Learning may reorder only inside this pre-existing priority band.
        enriched.sort(
            key=lambda row: (
                -float(row["learned_adjustment"]),
                str(row["provider_id"]),
            )
        )
        result.extend(enriched)

    return result, learned_any


def build_router_advisory(
    root: Path,
    *,
    capability: str | None = None,
    domain: str | None = None,
    allow_metered: bool = False,
    environ: Mapping[str, str] | None = None,
    health_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Build a deterministic provider ordering suggestion without routing traffic."""
    root = Path(root).resolve()
    env = os.environ if environ is None else environ
    candidates = list_providers(capability=capability, domain=domain)

    snapshot = (
        build_provider_health_snapshot(
            root,
            allow_metered=allow_metered,
            environ=env,
        )
        if health_snapshot is None
        else dict(health_snapshot)
    )
    health_by_id = _health_rows(snapshot)

    recommended: list[dict[str, object]] = []
    blocked: list[dict[str, object]] = []

    for descriptor in candidates:
        health = health_by_id.get(descriptor.provider_id)
        if health is None:
            blocked.append({
                "provider_id": descriptor.provider_id,
                "display_name": descriptor.display_name,
                "reason": "health_missing",
                "health": "unknown",
                "cost_class": descriptor.cost_class,
                "available_now": False,
            })
            continue

        available_now = bool(health.get("available_now", False))
        row = {
            "provider_id": descriptor.provider_id,
            "display_name": descriptor.display_name,
            "health": str(health.get("health") or "unknown"),
            "cost_class": descriptor.cost_class,
            "available_now": available_now,
            "historical_unreliable": bool(health.get("historical_unreliable", False)),
            "cooldown_active": bool(health.get("cooldown_active", False)),
            "policy_reason": str(health.get("policy_reason") or "unknown")[:80],
        }

        if available_now:
            recommended.append(row)
        else:
            blocked.append({
                **row,
                "reason": str(health.get("health") or "unavailable")[:80],
            })

    recommended, strategy_learning_applied = _apply_strategy_learning(
        root, capability, recommended
    )
    blocked.sort(key=lambda row: str(row["provider_id"]))

    return {
        "schema": ADVISORY_SCHEMA,
        "advisory_only": True,
        "routing_mutated": False,
        "strategy_learning_applied": strategy_learning_applied,
        "api_requests": 0,
        "paid_spending": False,
        "filters": {
            "capability": capability,
            "domain": domain,
            "allow_metered": allow_metered,
        },
        "candidate_count": len(candidates),
        "recommended_count": len(recommended),
        "blocked_count": len(blocked),
        "recommended_provider_ids": [str(row["provider_id"]) for row in recommended],
        "blocked_provider_ids": [str(row["provider_id"]) for row in blocked],
        "recommendations": recommended,
        "blocked": blocked,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only provider router advisory. "
            "Produces an ordering suggestion but never routes requests."
        )
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--capability")
    parser.add_argument("--domain")
    parser.add_argument("--allow-metered", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    advisory = build_router_advisory(
        Path(args.root),
        capability=args.capability,
        domain=args.domain,
        allow_metered=args.allow_metered,
    )
    print(json.dumps(advisory, indent=2, sort_keys=True))
    return 0 if advisory["recommended_count"] > 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
