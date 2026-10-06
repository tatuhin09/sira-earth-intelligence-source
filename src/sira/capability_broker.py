"""Capability Broker v1.6A.

Composes existing catalog, health, policy, learned router ordering, and access
metadata into one bounded decision. The broker never executes a provider,
creates an access request, spends money, or grants authority.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .access_requests import AccessNeed, KIND_CREDENTIAL, RISK_LOW
from .provider_catalog import ACCESS_KEY_REQUIRED, COST_METERED, ProviderDescriptor, list_providers
from .provider_router_advisory import build_router_advisory
from .storage import write_json

BROKER_SCHEMA = "sira.capability_broker_decision.v1"
BROKER_POLICY_VERSION = 1
MAX_CAPABILITY = 128

STATUS_READY = "ready"
STATUS_CAPABILITY_UNAVAILABLE = "capability_unavailable"
STATUS_CREDENTIAL_REQUIRED = "credential_required"
STATUS_METERED_POLICY_BLOCKED = "metered_policy_blocked"
STATUS_METERED_BUDGET_BLOCKED = "metered_budget_blocked"
STATUS_TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
STATUS_PROVIDER_UNAVAILABLE = "provider_unavailable"


def _clean_capability(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("capability must be a string")
    clean = value.strip()
    if not clean or len(clean) > MAX_CAPABILITY or any(ch.isspace() for ch in clean):
        raise ValueError("capability must be a stable bounded token")
    return clean


def _blocked_by_id(advisory: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = advisory.get("blocked")
    if not isinstance(rows, list):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if isinstance(row, Mapping) and isinstance(row.get("provider_id"), str):
            result[str(row["provider_id"])] = row
    return result


def _credential_access_need(
    descriptor: ProviderDescriptor,
    *,
    capability: str,
) -> dict[str, object] | None:
    if descriptor.access != ACCESS_KEY_REQUIRED or not descriptor.credential_env:
        return None
    credential_name = descriptor.credential_env[0]
    return AccessNeed(
        kind=KIND_CREDENTIAL,
        resource=f"provider:{descriptor.provider_id}",
        reason=(
            f"Capability {capability} has no currently usable provider because "
            f"{descriptor.display_name} requires a configured credential."
        ),
        risk=RISK_LOW,
        provider_id=descriptor.provider_id,
        credential_name=credential_name,
        owner_action=(
            f"Configure {credential_name} locally, approve the request, then let "
            "SIRA verify provisioning without exposing the secret value."
        ),
    ).to_dict()


def _blocked_summary(
    descriptors: tuple[ProviderDescriptor, ...],
    advisory: Mapping[str, Any],
    *,
    allow_metered: bool,
) -> list[dict[str, object]]:
    blocked = _blocked_by_id(advisory)
    rows: list[dict[str, object]] = []
    for descriptor in descriptors:
        row = blocked.get(descriptor.provider_id)
        if row is None:
            continue
        rows.append({
            "provider_id": descriptor.provider_id,
            "cost_class": descriptor.cost_class,
            "access": descriptor.access,
            "health": str(row.get("health") or "unknown")[:80],
            "reason": str(row.get("reason") or row.get("policy_reason") or "unavailable")[:80],
            "policy_reason": str(row.get("policy_reason") or "unknown")[:80],
            "cooldown_active": bool(row.get("cooldown_active", False)),
            "allow_metered": allow_metered,
        })
    rows.sort(key=lambda item: str(item["provider_id"]))
    return rows


def _classify_blocked(
    descriptors: tuple[ProviderDescriptor, ...],
    blocked: list[dict[str, object]],
    *,
    capability: str,
    allow_metered: bool,
) -> tuple[str, dict[str, object] | None]:
    by_id = {str(row["provider_id"]): row for row in blocked}

    if not allow_metered and descriptors and all(
        descriptor.cost_class == COST_METERED for descriptor in descriptors
    ):
        return STATUS_METERED_POLICY_BLOCKED, None

    for descriptor in descriptors:
        row = by_id.get(descriptor.provider_id, {})
        if (
            descriptor.access == ACCESS_KEY_REQUIRED
            and str(row.get("policy_reason") or "") == "required_credential_missing"
        ):
            need = _credential_access_need(descriptor, capability=capability)
            if need is not None:
                return STATUS_CREDENTIAL_REQUIRED, need

    if any(
        str(row.get("health") or "") == "metered_budget_blocked"
        for row in blocked
    ):
        return STATUS_METERED_BUDGET_BLOCKED, None

    if any(bool(row.get("cooldown_active", False)) for row in blocked):
        return STATUS_TEMPORARILY_UNAVAILABLE, None

    return STATUS_PROVIDER_UNAVAILABLE, None


def _sanitize_recommendations(
    advisory: Mapping[str, Any],
) -> list[dict[str, object]]:
    rows = advisory.get("recommendations")
    if not isinstance(rows, list):
        return []
    result: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        provider_id = row.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        result.append({
            "provider_id": provider_id,
            "health": str(row.get("health") or "unknown")[:80],
            "cost_class": str(row.get("cost_class") or "unknown")[:40],
            "historical_unreliable": bool(row.get("historical_unreliable", False)),
            "cooldown_active": bool(row.get("cooldown_active", False)),
            "learned_adjustment": float(row.get("learned_adjustment") or 0.0),
            "strategy_evidence_events": int(row.get("strategy_evidence_events") or 0),
        })
    return result


def broker_decision(
    root: Path,
    capability: str,
    *,
    allow_metered: bool = False,
    environ: Mapping[str, str] | None = None,
    health_snapshot: Mapping[str, Any] | None = None,
    persist: bool = False,
) -> dict[str, object]:
    root = Path(root).resolve()
    capability = _clean_capability(capability)
    env = os.environ if environ is None else environ
    descriptors = list_providers(capability=capability, autonomous_only=True)

    decision_id = "cb_" + uuid4().hex
    if not descriptors:
        decision: dict[str, object] = {
            "schema": BROKER_SCHEMA,
            "policy_version": BROKER_POLICY_VERSION,
            "decision_id": decision_id,
            "capability": capability,
            "status": STATUS_CAPABILITY_UNAVAILABLE,
            "selected_provider_id": None,
            "fallback_provider_ids": [],
            "candidate_provider_ids": [],
            "recommendations": [],
            "blocked": [],
            "access_need": None,
            "owner_action_required": False,
            "allow_metered": allow_metered,
            "strategy_learning_applied": False,
            "api_requests": 0,
            "paid_spending": False,
            "provider_execution_performed": False,
            "access_request_created": False,
            "authority_granted": False,
            "promotion_authorized": False,
            "artifact": None,
        }
    else:
        advisory = build_router_advisory(
            root,
            capability=capability,
            allow_metered=allow_metered,
            environ=env,
            health_snapshot=health_snapshot,
        )
        recommended = _sanitize_recommendations(advisory)
        blocked = _blocked_summary(descriptors, advisory, allow_metered=allow_metered)
        if recommended:
            status, access_need = STATUS_READY, None
        else:
            status, access_need = _classify_blocked(
                descriptors,
                blocked,
                capability=capability,
                allow_metered=allow_metered,
            )

        chain = [str(row["provider_id"]) for row in recommended]
        decision = {
            "schema": BROKER_SCHEMA,
            "policy_version": BROKER_POLICY_VERSION,
            "decision_id": decision_id,
            "capability": capability,
            "status": status,
            "selected_provider_id": chain[0] if chain else None,
            "fallback_provider_ids": chain[1:],
            "candidate_provider_ids": [item.provider_id for item in descriptors],
            "recommendations": recommended,
            "blocked": blocked,
            "access_need": access_need,
            "owner_action_required": access_need is not None,
            "allow_metered": allow_metered,
            "strategy_learning_applied": bool(advisory.get("strategy_learning_applied", False)),
            "api_requests": 0,
            "paid_spending": False,
            "provider_execution_performed": False,
            "access_request_created": False,
            "authority_granted": False,
            "promotion_authorized": False,
            "artifact": None,
        }

    if persist:
        base = root / "runtime" / "capability_broker" / "decisions"
        base.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = base / f"{decision_id}.json"
        decision["artifact"] = str(path)
        write_json(path, decision)

    return decision
