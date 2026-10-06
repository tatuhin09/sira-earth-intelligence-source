from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import json
import os
from pathlib import Path
import sys
from typing import Mapping

if __package__ in {None, ""}:
    ROOT = Path(__file__).resolve().parents[2]
    SRC = ROOT / "src"
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

from sira.provider_catalog import (
    COST_FREE,
    COST_METERED,
    ProviderDescriptor,
    list_providers,
)
from sira.provider_readiness import (
    ProviderReadiness,
    assess_provider,
)


STATUS_SELECTED = "selected"
STATUS_BLOCKED = "blocked"

REASON_READY = "ready"
REASON_METERED_DISABLED = "metered_disabled"
REASON_REQUIRED_CREDENTIAL_MISSING = "required_credential_missing"
REASON_AUTONOMOUS_DISABLED = "autonomous_research_disabled"


@dataclass(frozen=True, slots=True)
class ProviderPolicyDecision:
    provider_id: str
    display_name: str
    status: str
    selected: bool
    reason: str
    cost_class: str
    access: str
    credential_required: bool
    credential_configured: bool
    autonomous_research_allowed: bool
    domains: tuple[str, ...]
    capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["domains"] = list(self.domains)
        value["capabilities"] = list(self.capabilities)
        return value


def _decision_for(
    descriptor: ProviderDescriptor,
    readiness: ProviderReadiness,
    *,
    allow_metered: bool,
    autonomous_only: bool,
) -> ProviderPolicyDecision:
    selected = True
    reason = REASON_READY

    if not readiness.usable:
        selected = False
        reason = REASON_REQUIRED_CREDENTIAL_MISSING
    elif autonomous_only and not descriptor.autonomous_research_allowed:
        selected = False
        reason = REASON_AUTONOMOUS_DISABLED
    elif descriptor.cost_class == COST_METERED and not allow_metered:
        selected = False
        reason = REASON_METERED_DISABLED

    return ProviderPolicyDecision(
        provider_id=descriptor.provider_id,
        display_name=descriptor.display_name,
        status=STATUS_SELECTED if selected else STATUS_BLOCKED,
        selected=selected,
        reason=reason,
        cost_class=descriptor.cost_class,
        access=descriptor.access,
        credential_required=readiness.credential_required,
        credential_configured=readiness.credential_configured,
        autonomous_research_allowed=descriptor.autonomous_research_allowed,
        domains=descriptor.domains,
        capabilities=descriptor.capabilities,
    )


def build_provider_plan(
    *,
    capability: str | None = None,
    domain: str | None = None,
    allow_metered: bool = False,
    autonomous_only: bool = True,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    env = os.environ if environ is None else environ

    descriptors = list_providers(
        domain=domain,
        capability=capability,
    )

    decisions = [
        _decision_for(
            descriptor,
            assess_provider(descriptor, environ=env),
            allow_metered=allow_metered,
            autonomous_only=autonomous_only,
        )
        for descriptor in descriptors
    ]

    decisions.sort(
        key=lambda item: (
            not item.selected,
            item.cost_class != COST_FREE,
            item.provider_id,
        )
    )

    selected = [item for item in decisions if item.selected]
    blocked = [item for item in decisions if not item.selected]

    return {
        "schema": "sira.provider_policy.v1",
        "filters": {
            "capability": capability,
            "domain": domain,
            "allow_metered": allow_metered,
            "autonomous_only": autonomous_only,
        },
        "candidate_count": len(decisions),
        "selected_count": len(selected),
        "blocked_count": len(blocked),
        "selected_provider_ids": [item.provider_id for item in selected],
        "decisions": [item.to_dict() for item in decisions],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic local provider-selection policy. "
            "No network requests are made."
        )
    )
    parser.add_argument("--capability")
    parser.add_argument("--domain")
    parser.add_argument(
        "--allow-metered",
        action="store_true",
        help="Allow metered providers to be selected if otherwise ready.",
    )
    parser.add_argument(
        "--include-nonautonomous",
        action="store_true",
        help="Include providers not approved for autonomous research.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    snapshot = build_provider_plan(
        capability=args.capability,
        domain=args.domain,
        allow_metered=args.allow_metered,
        autonomous_only=not args.include_nonautonomous,
    )

    print(json.dumps(snapshot, indent=2, sort_keys=True))

    # Empty candidate sets and fully-blocked candidate sets are explicit,
    # machine-readable "no usable provider" outcomes.
    return 0 if snapshot["selected_count"] > 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
