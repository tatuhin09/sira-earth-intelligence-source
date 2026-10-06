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
    ACCESS_KEY_REQUIRED,
    ACCESS_OPTIONAL_KEY,
    COST_METERED,
    ProviderDescriptor,
    get_provider,
    list_providers,
)


STATE_READY = "ready"
STATE_OPTIONAL_CREDENTIAL_MISSING = "ready_optional_credential_missing"
STATE_REQUIRED_CREDENTIAL_MISSING = "blocked_required_credential_missing"


@dataclass(frozen=True, slots=True)
class ProviderReadiness:
    provider_id: str
    display_name: str
    state: str
    usable: bool
    access: str
    cost_class: str
    network: bool
    read_only: bool
    autonomous_research_allowed: bool
    credential_names: tuple[str, ...]
    credential_configured: bool
    credential_required: bool
    credential_source: str = "process_environment"

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["credential_names"] = list(self.credential_names)
        return value


def _credential_configured(
    descriptor: ProviderDescriptor,
    environ: Mapping[str, str],
) -> bool:
    if not descriptor.credential_env:
        return False
    return all(bool(environ.get(name, "").strip()) for name in descriptor.credential_env)


def assess_provider(
    descriptor: ProviderDescriptor,
    *,
    environ: Mapping[str, str] | None = None,
) -> ProviderReadiness:
    env = os.environ if environ is None else environ
    configured = _credential_configured(descriptor, env)
    required = descriptor.access == ACCESS_KEY_REQUIRED

    if required and not configured:
        state = STATE_REQUIRED_CREDENTIAL_MISSING
        usable = False
    elif descriptor.access == ACCESS_OPTIONAL_KEY and not configured:
        state = STATE_OPTIONAL_CREDENTIAL_MISSING
        usable = True
    else:
        state = STATE_READY
        usable = True

    return ProviderReadiness(
        provider_id=descriptor.provider_id,
        display_name=descriptor.display_name,
        state=state,
        usable=usable,
        access=descriptor.access,
        cost_class=descriptor.cost_class,
        network=descriptor.network,
        read_only=descriptor.read_only,
        autonomous_research_allowed=descriptor.autonomous_research_allowed,
        credential_names=descriptor.credential_env,
        credential_configured=configured,
        credential_required=required,
    )


def readiness_snapshot(
    *,
    environ: Mapping[str, str] | None = None,
    provider_id: str | None = None,
) -> dict[str, object]:
    descriptors = (
        (get_provider(provider_id),)
        if provider_id is not None
        else list_providers()
    )
    rows = tuple(
        assess_provider(item, environ=environ)
        for item in descriptors
    )

    return {
        "schema": "sira.provider_readiness.v1",
        "credential_source": "process_environment",
        "provider_count": len(rows),
        "usable_count": sum(1 for item in rows if item.usable),
        "blocked_count": sum(1 for item in rows if not item.usable),
        "credential_configured_count": sum(
            1 for item in rows if item.credential_configured
        ),
        "metered_count": sum(
            1 for item in rows if item.cost_class == COST_METERED
        ),
        "providers": [item.to_dict() for item in rows],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Local provider readiness diagnostic. "
            "Reports credential presence only; never credential values."
        )
    )
    parser.add_argument(
        "--provider",
        help="Optional provider_id to inspect instead of the full catalog.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        snapshot = readiness_snapshot(provider_id=args.provider)
    except KeyError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "unknown_provider",
                    "message": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0 if snapshot["blocked_count"] == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
