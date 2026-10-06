from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping

if __package__ in {None, ""}:
    ROOT = Path(__file__).resolve().parents[2]
    SRC = ROOT / "src"
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

from sira.opportunity_history import collect_history_context
from sira.provider_catalog import COST_METERED, ProviderDescriptor, list_providers
from sira.provider_policy import build_provider_plan
from sira.runtime_reliability import RuntimeReliabilityStore


HEALTH_READY = "ready"
HEALTH_POLICY_BLOCKED = "policy_blocked"
HEALTH_COOLDOWN = "provider_cooldown"
HEALTH_METERED_BUDGET_BLOCKED = "metered_budget_blocked"
HEALTH_DEGRADED_HISTORY = "degraded_history"

MAX_COOLDOWN_FILES = 64
MAX_COOLDOWN_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider_id: str
    display_name: str
    health: str
    available_now: bool
    policy_selected: bool
    policy_reason: str
    cost_class: str
    cooldown_active: bool
    cooldown_code: str | None
    cooldown_remaining_seconds: float
    historical_unreliable: bool
    historical_failures: int
    historical_successes: int
    historical_samples: int
    historical_capabilities: tuple[str, ...]
    metered_budget_allowed: bool | None
    metered_budget_reason: str | None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["historical_capabilities"] = list(self.historical_capabilities)
        return value


def _normalize_provider_name(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _provider_alias_map(
    descriptors: tuple[ProviderDescriptor, ...],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for descriptor in descriptors:
        candidates = {
            descriptor.provider_id,
            descriptor.provider_id.replace("_", " "),
            descriptor.display_name,
        }
        for candidate in candidates:
            normalized = _normalize_provider_name(candidate)
            if normalized:
                aliases[normalized] = descriptor.provider_id
    return aliases


def _safe_cooldown_payload(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_COOLDOWN_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def active_provider_cooldowns(
    root: Path,
    *,
    now_epoch: float | None = None,
    descriptors: tuple[ProviderDescriptor, ...] | None = None,
) -> dict[str, dict[str, object]]:
    """Read active provider cooldown artifacts without mutating or probing providers."""
    root = Path(root).resolve()
    now = time.time() if now_epoch is None else float(now_epoch)
    known = list_providers() if descriptors is None else descriptors
    aliases = _provider_alias_map(known)
    cooldown_dir = root / ".cache" / "opportunity_provider_cooldowns"

    if cooldown_dir.is_symlink() or not cooldown_dir.is_dir():
        return {}

    result: dict[str, dict[str, object]] = {}
    try:
        paths = sorted(cooldown_dir.glob("*.json"))[:MAX_COOLDOWN_FILES]
    except OSError:
        return {}

    for path in paths:
        value = _safe_cooldown_payload(path)
        if value is None:
            continue

        provider = value.get("provider")
        code = value.get("code")
        until = value.get("until_epoch")
        recorded = value.get("recorded_at_epoch")

        if not isinstance(provider, str) or not isinstance(code, str):
            continue
        if type(until) not in (int, float) or isinstance(until, bool):
            continue

        provider_id = aliases.get(_normalize_provider_name(provider))
        if provider_id is None:
            continue

        remaining = float(until) - now
        if remaining <= 0:
            continue

        row = {
            "provider_id": provider_id,
            "provider_name": provider,
            "code": code[:80],
            "remaining_seconds": round(remaining, 3),
            "until_epoch": float(until),
            "recorded_at_epoch": (
                float(recorded)
                if type(recorded) in (int, float) and not isinstance(recorded, bool)
                else None
            ),
        }

        previous = result.get(provider_id)
        if previous is None or float(row["until_epoch"]) > float(previous["until_epoch"]):
            result[provider_id] = row

    return result


def _historical_provider_health(
    history: Mapping[str, Any],
    descriptors: tuple[ProviderDescriptor, ...],
) -> dict[str, dict[str, object]]:
    aliases = _provider_alias_map(descriptors)
    reliability = history.get("provider_reliability")
    if not isinstance(reliability, Mapping):
        return {}

    aggregates: dict[str, dict[str, Any]] = {}
    for capability, block in reliability.items():
        if not isinstance(capability, str) or not isinstance(block, Mapping):
            continue
        rows = block.get("recurring_unreliable_providers")
        if not isinstance(rows, list):
            continue

        for row in rows[:20]:
            if not isinstance(row, Mapping):
                continue
            provider = row.get("provider")
            if not isinstance(provider, str):
                continue
            provider_id = aliases.get(_normalize_provider_name(provider))
            if provider_id is None:
                continue

            failures = row.get("failures")
            successes = row.get("successes")
            samples = row.get("samples")
            if not all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in (failures, successes, samples)
            ):
                continue

            target = aggregates.setdefault(
                provider_id,
                {
                    "failures": 0,
                    "successes": 0,
                    "samples": 0,
                    "capabilities": set(),
                },
            )
            target["failures"] += failures
            target["successes"] += successes
            target["samples"] += samples
            target["capabilities"].add(capability)

    result: dict[str, dict[str, object]] = {}
    for provider_id, row in aggregates.items():
        result[provider_id] = {
            "failures": int(row["failures"]),
            "successes": int(row["successes"]),
            "samples": int(row["samples"]),
            "capabilities": sorted(row["capabilities"]),
        }
    return result


def build_provider_health_snapshot(
    root: Path,
    *,
    now_epoch: float | None = None,
    allow_metered: bool = False,
    environ: Mapping[str, str] | None = None,
    history: Mapping[str, Any] | None = None,
    runtime_status: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Compose read-only provider health from existing local SIRA state."""
    root = Path(root).resolve()
    now = time.time() if now_epoch is None else float(now_epoch)
    env = os.environ if environ is None else environ
    descriptors = list_providers()

    plan = build_provider_plan(
        allow_metered=allow_metered,
        autonomous_only=True,
        environ=env,
    )
    decisions = {
        row["provider_id"]: row
        for row in plan["decisions"]
        if isinstance(row, dict) and isinstance(row.get("provider_id"), str)
    }

    cooldowns = active_provider_cooldowns(
        root,
        now_epoch=now,
        descriptors=descriptors,
    )

    local_history = (
        collect_history_context(root)
        if history is None
        else dict(history)
    )
    historical = _historical_provider_health(local_history, descriptors)

    runtime = (
        RuntimeReliabilityStore(root).status(now_epoch=now)
        if runtime_status is None
        else dict(runtime_status)
    )
    budget = runtime.get("metered_budget")
    if not isinstance(budget, Mapping):
        budget = {}
    metered_allowed = bool(budget.get("allowed", False))
    metered_reason = str(budget.get("reason") or "unknown")[:80]

    rows: list[ProviderHealth] = []
    for descriptor in descriptors:
        decision = decisions.get(descriptor.provider_id, {})
        policy_selected = bool(decision.get("selected", False))
        policy_reason = str(decision.get("reason") or "not_selected")[:80]

        cooldown = cooldowns.get(descriptor.provider_id)
        history_row = historical.get(descriptor.provider_id)
        cooldown_active = cooldown is not None
        historical_unreliable = history_row is not None
        is_metered = descriptor.cost_class == COST_METERED

        if not policy_selected:
            health = HEALTH_POLICY_BLOCKED
            available = False
        elif cooldown_active:
            health = HEALTH_COOLDOWN
            available = False
        elif is_metered and not metered_allowed:
            health = HEALTH_METERED_BUDGET_BLOCKED
            available = False
        elif historical_unreliable:
            health = HEALTH_DEGRADED_HISTORY
            available = True
        else:
            health = HEALTH_READY
            available = True

        rows.append(
            ProviderHealth(
                provider_id=descriptor.provider_id,
                display_name=descriptor.display_name,
                health=health,
                available_now=available,
                policy_selected=policy_selected,
                policy_reason=policy_reason,
                cost_class=descriptor.cost_class,
                cooldown_active=cooldown_active,
                cooldown_code=(
                    str(cooldown.get("code"))[:80]
                    if cooldown is not None
                    else None
                ),
                cooldown_remaining_seconds=(
                    float(cooldown.get("remaining_seconds") or 0.0)
                    if cooldown is not None
                    else 0.0
                ),
                historical_unreliable=historical_unreliable,
                historical_failures=(
                    int(history_row.get("failures") or 0)
                    if history_row is not None
                    else 0
                ),
                historical_successes=(
                    int(history_row.get("successes") or 0)
                    if history_row is not None
                    else 0
                ),
                historical_samples=(
                    int(history_row.get("samples") or 0)
                    if history_row is not None
                    else 0
                ),
                historical_capabilities=(
                    tuple(history_row.get("capabilities") or ())
                    if history_row is not None
                    else ()
                ),
                metered_budget_allowed=(
                    metered_allowed if is_metered else None
                ),
                metered_budget_reason=(
                    metered_reason if is_metered else None
                ),
            )
        )

    rows.sort(key=lambda item: item.provider_id)

    return {
        "schema": "sira.provider_health.v1",
        "generated_at_epoch": now,
        "api_requests": 0,
        "paid_spending": False,
        "provider_count": len(rows),
        "available_count": sum(1 for row in rows if row.available_now),
        "blocked_count": sum(1 for row in rows if not row.available_now),
        "degraded_count": sum(
            1 for row in rows if row.health == HEALTH_DEGRADED_HISTORY
        ),
        "active_cooldown_count": sum(
            1 for row in rows if row.cooldown_active
        ),
        "global_runtime": {
            "policy_version": runtime.get("policy_version"),
            "transient_streak": runtime.get("transient_streak"),
            "next_cycle_in_seconds": runtime.get("next_cycle_in_seconds"),
            "last_failure_class": runtime.get("last_failure_class"),
            "last_outcome": runtime.get("last_outcome"),
            "metered_budget": dict(budget),
        },
        "providers": [row.to_dict() for row in rows],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only local provider health snapshot. "
            "No provider network requests are made."
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
            "Make metered providers policy-eligible; the existing runtime "
            "metered budget can still block them."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    snapshot = build_provider_health_snapshot(
        Path(args.root),
        allow_metered=args.allow_metered,
    )
    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
