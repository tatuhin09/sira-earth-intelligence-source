from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .access_requests import (
    AccessNeed,
    AccessRequestStore,
    KIND_CREDENTIAL,
    KIND_PERMISSION,
    RISK_MODERATE,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_SATISFIED,
)
from .provider_catalog import ProviderDescriptor, list_providers


_ACCESS_FAILURE_CODES = {
    "missing_key",
    "credential_error",
    "authentication_error",
    "auth_error",
    "required_credential_missing",
    "blocked_required_credential_missing",
}


def target_identity(target: Mapping[str, Any]) -> str:
    kind = target.get("target_kind")
    if kind == "memory":
        value = target.get("memory_id")
        if isinstance(value, str) and value:
            return f"memory:{value}"
    elif kind == "opportunity":
        value = target.get("opportunity_id")
        if isinstance(value, str) and value:
            return f"opportunity:{value}"
    elif kind == "knowledge_revalidation":
        value = target.get("knowledge_key")
        if isinstance(value, str) and value:
            return f"knowledge_revalidation:{value}"
    elif kind == "learning_goal":
        value = target.get("learning_goal_id")
        if isinstance(value, str) and value:
            return f"learning_goal:{value}"
    raise ValueError("autonomous target identity is unavailable")


def _descriptor_for_provider(value: object) -> ProviderDescriptor | None:
    if not isinstance(value, str) or not value.strip():
        return None
    key = value.strip().casefold().replace("-", "_").replace(" ", "_")
    for descriptor in list_providers():
        aliases = {
            descriptor.provider_id.casefold().replace("-", "_").replace(" ", "_"),
            descriptor.display_name.casefold().replace("-", "_").replace(" ", "_"),
        }
        if key in aliases:
            return descriptor
    return None


def access_need_from_research(research: Mapping[str, Any]) -> AccessNeed | None:
    failures = research.get("provider_failures")
    if not isinstance(failures, list):
        return None

    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        code = failure.get("code")
        if not isinstance(code, str) or code not in _ACCESS_FAILURE_CODES:
            continue

        provider = failure.get("provider")
        descriptor = _descriptor_for_provider(provider)
        if descriptor is not None and descriptor.credential_env:
            credential_name = descriptor.credential_env[0]
            return AccessNeed(
                kind=KIND_CREDENTIAL,
                resource=descriptor.provider_id,
                reason=(
                    f"{descriptor.display_name} cannot continue this research task "
                    f"because provider access failed with {code}."
                ),
                risk=RISK_MODERATE,
                provider_id=descriptor.provider_id,
                credential_name=credential_name,
                owner_action=(
                    f"Provision or refresh {credential_name} through the approved "
                    "secret channel, then mark this request satisfied."
                ),
            )

        resource = (
            str(provider).strip()
            if isinstance(provider, str) and provider.strip()
            else "external_provider"
        )
        return AccessNeed(
            kind=KIND_PERMISSION,
            resource=resource,
            reason=f"External provider access failed with {code}.",
            risk=RISK_MODERATE,
            owner_action=(
                "Provide the required legitimate access or permission, "
                "then mark this request satisfied."
            ),
        )
    return None



def access_need_from_broker_decision(
    decision: Mapping[str, Any],
) -> AccessNeed | None:
    """Convert metadata-only broker access need into the existing access contract."""
    raw = decision.get("access_need")
    if not isinstance(raw, Mapping):
        return None

    kind = raw.get("kind")
    resource = raw.get("resource")
    reason = raw.get("reason")
    if not isinstance(kind, str) or not isinstance(resource, str) or not isinstance(reason, str):
        return None

    provider_id = raw.get("provider_id")
    credential_name = raw.get("credential_name")
    owner_action = raw.get("owner_action")
    risk = raw.get("risk")

    need = AccessNeed(
        kind=kind,
        resource=resource,
        reason=reason,
        risk=risk if isinstance(risk, str) else RISK_MODERATE,
        provider_id=provider_id if isinstance(provider_id, str) else None,
        credential_name=credential_name if isinstance(credential_name, str) else None,
        owner_action=owner_action if isinstance(owner_action, str) else None,
    )
    try:
        need.validate()
    except ValueError:
        return None
    return need


def blocked_target_ids(root: Path) -> set[str]:
    blocked: set[str] = set()
    store = AccessRequestStore(root)
    for row in store.iter_records():
        if row.get("status") not in {STATUS_PENDING, STATUS_APPROVED}:
            continue
        task = row.get("blocked_task")
        if not isinstance(task, Mapping) or task.get("task_kind") != "autonomous_target":
            continue
        identity = task.get("task_id")
        if isinstance(identity, str) and identity:
            blocked.add(identity)
    return blocked


def filter_selection_for_access(
    selection: Mapping[str, Any],
    blocked_ids: set[str],
) -> dict[str, Any]:
    result = dict(selection)
    target = result.get("target") if isinstance(result.get("target"), Mapping) else None
    alternatives = (
        result.get("alternatives")
        if isinstance(result.get("alternatives"), list)
        else []
    )

    pool: list[dict[str, Any]] = []
    if target is not None:
        pool.append(dict(target))
    pool.extend(dict(row) for row in alternatives if isinstance(row, Mapping))

    kept: list[dict[str, Any]] = []
    removed: list[str] = []
    for candidate in pool:
        try:
            identity = target_identity(candidate)
        except ValueError:
            kept.append(candidate)
            continue
        if identity in blocked_ids:
            removed.append(identity)
        else:
            kept.append(candidate)

    result["access_blocked_target_ids"] = sorted(set(removed))
    result["access_reselected"] = bool(
        target is not None and kept and dict(target) != kept[0]
    )
    result["target"] = kept[0] if kept else None
    result["alternatives"] = kept[1:10]
    if kept:
        result["status"] = "selected"
    elif removed:
        result["status"] = "idle_access_blocked"
    return result


def park_target_for_access(
    root: Path,
    target: Mapping[str, Any],
    need: AccessNeed,
    *,
    worker_task_id: str | None = None,
) -> dict[str, Any]:
    identity = target_identity(target)
    hint = "Freshly reselect this autonomous target after required access is available."
    if isinstance(worker_task_id, str) and worker_task_id:
        hint += f" Prior worker task: {worker_task_id}."
    return AccessRequestStore(root).create(
        need,
        task_kind="autonomous_target",
        task_id=identity,
        resume_hint=hint,
    )


def consume_resume_for_target(
    root: Path,
    target: Mapping[str, Any] | None,
) -> list[str]:
    if target is None:
        return []
    identity = target_identity(target)
    store = AccessRequestStore(root)
    consumed: list[str] = []
    for row in store.resume_ready():
        task = row.get("blocked_task")
        if (
            isinstance(task, Mapping)
            and task.get("task_kind") == "autonomous_target"
            and task.get("task_id") == identity
            and row.get("status") == STATUS_SATISFIED
        ):
            request_id = row.get("request_id")
            if isinstance(request_id, str):
                store.consume_resume(request_id)
                consumed.append(request_id)
    return consumed
