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

from sira.provider_catalog import catalog_snapshot, list_providers
from sira.provider_health import build_provider_health_snapshot
from sira.provider_readiness import readiness_snapshot
from sira.provider_router_advisory import build_router_advisory


DIAGNOSTICS_SCHEMA = "sira.provider_diagnostics.v1"


def _capabilities() -> list[str]:
    values = {
        capability
        for provider in list_providers()
        for capability in provider.capabilities
    }
    return sorted(values)


def build_provider_diagnostics(
    root: Path,
    *,
    allow_metered: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Build one read-only provider diagnostics payload for UI/control surfaces."""
    root = Path(root).resolve()
    env = os.environ if environ is None else environ

    catalog = catalog_snapshot()
    readiness = readiness_snapshot(environ=env)
    health = build_provider_health_snapshot(
        root,
        allow_metered=allow_metered,
        environ=env,
    )

    advisories: dict[str, object] = {}
    for capability in _capabilities():
        advisories[capability] = build_router_advisory(
            root,
            capability=capability,
            allow_metered=allow_metered,
            environ=env,
            health_snapshot=health,
        )

    blocked = [
        row
        for row in health.get("providers", [])
        if isinstance(row, dict) and not bool(row.get("available_now", False))
    ]
    degraded = [
        row
        for row in health.get("providers", [])
        if isinstance(row, dict) and bool(row.get("historical_unreliable", False))
    ]

    return {
        "schema": DIAGNOSTICS_SCHEMA,
        "read_only": True,
        "routing_mutated": False,
        "api_requests": 0,
        "paid_spending": False,
        "allow_metered": allow_metered,
        "summary": {
            "provider_count": int(health.get("provider_count") or 0),
            "available_count": int(health.get("available_count") or 0),
            "blocked_count": int(health.get("blocked_count") or 0),
            "degraded_count": int(health.get("degraded_count") or 0),
            "active_cooldown_count": int(health.get("active_cooldown_count") or 0),
            "capability_count": len(advisories),
            "blocked_provider_ids": sorted(
                str(row.get("provider_id"))
                for row in blocked
                if isinstance(row.get("provider_id"), str)
            ),
            "degraded_provider_ids": sorted(
                str(row.get("provider_id"))
                for row in degraded
                if isinstance(row.get("provider_id"), str)
            ),
        },
        "catalog": catalog,
        "readiness": readiness,
        "health": health,
        "advisories": advisories,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only consolidated provider diagnostics for SIRA. "
            "No provider network calls are made."
        )
    )
    parser.add_argument(
        "--root",
        default=".",
        help="SIRA repository root (default: current directory).",
    )
    parser.add_argument(
        "--allow-metered",
        action="store_true",
        help=(
            "Show diagnostics with metered providers policy-eligible where "
            "credentials and runtime budget also permit them."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = build_provider_diagnostics(
        Path(args.root),
        allow_metered=args.allow_metered,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
