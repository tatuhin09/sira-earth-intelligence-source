from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .access_requests import (
    AccessRequestStore,
    KIND_CREDENTIAL,
    STATUS_APPROVED,
    STATUS_SATISFIED,
)
from .config import SUPPORTED_CREDENTIALS, load_optional_key


VERIFY_SCHEMA = "sira.access_provisioning_verification.v1"
RECONCILE_SCHEMA = "sira.access_provisioning_reconcile.v1"


def _verification_payload(
    request_id: str,
    *,
    credential_name: str | None,
    verified: bool,
    reason: str,
    record_status: str,
) -> dict[str, object]:
    return {
        "schema": VERIFY_SCHEMA,
        "request_id": request_id,
        "kind": KIND_CREDENTIAL,
        "credential_name": credential_name,
        "verified": verified,
        "reason": reason,
        "record_status": record_status,
        "secret_value_returned": False,
    }


def verify_credential_request(
    root: Path,
    request_id: str,
) -> dict[str, object]:
    """Verify one approved credential request using local secret loading only.

    Secret values are held only long enough to validate presence/format and are
    never returned, persisted, logged, or copied into worker/task artifacts.
    """
    root = Path(root).resolve()
    store = AccessRequestStore(root)
    row = store.load(request_id)
    status = row.get("status")
    need = row.get("need")

    if not isinstance(need, Mapping) or need.get("kind") != KIND_CREDENTIAL:
        raise ValueError("access request is not a credential request")

    credential_name = need.get("credential_name")
    if not isinstance(credential_name, str) or not credential_name:
        raise ValueError("credential request is missing credential_name")

    if status == STATUS_SATISFIED:
        return _verification_payload(
            request_id,
            credential_name=credential_name,
            verified=True,
            reason="already_satisfied",
            record_status=STATUS_SATISFIED,
        )

    if status != STATUS_APPROVED:
        raise ValueError("credential request must be owner-approved before verification")

    if credential_name not in SUPPORTED_CREDENTIALS:
        return _verification_payload(
            request_id,
            credential_name=credential_name,
            verified=False,
            reason="unsupported_credential_verifier",
            record_status=str(status),
        )

    try:
        configured = load_optional_key(root, credential_name) is not None
    except (OSError, UnicodeError, ValueError):
        return _verification_payload(
            request_id,
            credential_name=credential_name,
            verified=False,
            reason="credential_source_invalid",
            record_status=str(status),
        )

    return _verification_payload(
        request_id,
        credential_name=credential_name,
        verified=configured,
        reason="credential_configured" if configured else "credential_missing",
        record_status=str(status),
    )


def verify_and_satisfy_access_request(
    root: Path,
    request_id: str,
) -> dict[str, Any]:
    """Verify a credential request, then mark it satisfied only on success."""
    root = Path(root).resolve()
    store = AccessRequestStore(root)
    verification = verify_credential_request(root, request_id)

    if verification["verified"] is not True:
        return {
            "schema": "sira.access_verified_satisfaction.v1",
            "status": verification["record_status"],
            "request_id": request_id,
            "satisfied": False,
            "verification": verification,
            "secret_value_returned": False,
        }

    row = store.load(request_id)
    if row.get("status") != STATUS_SATISFIED:
        row = store.mark_satisfied(request_id)

    result = dict(row)
    result["verification"] = verification
    result["verified_satisfaction"] = True
    result["secret_value_returned"] = False
    return result


def reconcile_approved_access_requests(root: Path) -> dict[str, object]:
    """Auto-satisfy only approved credential requests that verify locally."""
    root = Path(root).resolve()
    store = AccessRequestStore(root)

    checked = 0
    satisfied = 0
    waiting = 0
    unavailable = 0
    request_ids: list[str] = []

    for row in store.iter_records():
        if row.get("status") != STATUS_APPROVED:
            continue
        need = row.get("need")
        if not isinstance(need, Mapping) or need.get("kind") != KIND_CREDENTIAL:
            continue
        request_id = row.get("request_id")
        if not isinstance(request_id, str):
            continue

        checked += 1
        verification = verify_credential_request(root, request_id)
        if verification["verified"] is True:
            store.mark_satisfied(request_id)
            satisfied += 1
            request_ids.append(request_id)
        elif verification["reason"] == "unsupported_credential_verifier":
            unavailable += 1
        else:
            waiting += 1

    return {
        "schema": RECONCILE_SCHEMA,
        "checked": checked,
        "verified_satisfied": satisfied,
        "waiting_provision": waiting,
        "verification_unavailable": unavailable,
        "satisfied_request_ids": request_ids,
        "secret_values_returned": 0,
    }
