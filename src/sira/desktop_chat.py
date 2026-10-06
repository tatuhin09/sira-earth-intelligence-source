"""Protected model-backed conversation boundary for SIRA Desktop.

The desktop chat can read bounded runtime/memory context and request one
bounded model reply. It has no runtime, filesystem-mutation, promotion,
payment, package-install, or tool-execution authority.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4

from .access_requests import (
    AccessNeed,
    AccessRequestStore,
    KIND_CREDENTIAL,
    RISK_LOW,
)
from .capability_broker import (
    STATUS_CREDENTIAL_REQUIRED,
    STATUS_READY,
    broker_decision,
)
from .config import load_key, load_optional_key
from .memory import MemoryStore
from .models import ProviderError, utc_now
from .providers.gemini_chat import (
    DesktopChatBatch,
    GeminiDesktopChatModel,
)
from .runtime import RuntimeStateStore
from .storage import write_json

CAPABILITY = "desktop_chat"
CHAT_POLICY_VERSION = 1
MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_TEXT_CHARS = 12000
MAX_MEMORY_ROWS = 5
MAX_MEMORY_SUMMARY_CHARS = 600
MAX_REPLY_CHARS = 12000
FREE_TIER_CONFIRM_ENV = "SIRA_GEMINI_FREE_TIER_CONFIRMED"
MONTHLY_CAP_ENV = "SIRA_DESKTOP_CHAT_MONTHLY_CAP"
DEFAULT_MONTHLY_CAP = 100
MAX_MONTHLY_CAP = 1000


class DesktopChatModel(Protocol):
    def generate(
        self,
        payload: dict[str, object],
    ) -> DesktopChatBatch: ...


class DesktopChatSettingsStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = (
            self.root
            / "runtime"
            / "desktop"
            / "chat_settings.json"
        )

    def read(self) -> dict[str, object]:
        default = {
            "schema": "sira.desktop_chat_settings.v1",
            "gemini_free_tier_confirmed": False,
        }
        try:
            if (
                self.path.is_symlink()
                or not self.path.is_file()
                or self.path.stat().st_size > 16 * 1024
            ):
                return default
            value = json.loads(
                self.path.read_text(encoding="utf-8")
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            return default
        if (
            not isinstance(value, dict)
            or value.get("schema")
            != "sira.desktop_chat_settings.v1"
            or type(
                value.get("gemini_free_tier_confirmed")
            )
            is not bool
        ):
            return default
        return {
            "schema": "sira.desktop_chat_settings.v1",
            "gemini_free_tier_confirmed": bool(
                value["gemini_free_tier_confirmed"]
            ),
        }

    def set_free_tier_confirmed(
        self,
        confirmed: bool,
    ) -> dict[str, object]:
        if type(confirmed) is not bool:
            raise ValueError(
                "free-tier confirmation must be boolean"
            )
        payload = {
            "schema": "sira.desktop_chat_settings.v1",
            "gemini_free_tier_confirmed": confirmed,
            "updated_at": utc_now(),
            "paid_spending_authorized": False,
            "billing_changes_authorized": False,
        }
        write_json(self.path, payload)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return payload


def _truthy(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.strip().casefold()
        in {"1", "true", "yes", "on"}
    )


def _month() -> str:
    return utc_now()[:7]


def _cap(
    env: Mapping[str, str],
) -> int:
    raw = str(env.get(MONTHLY_CAP_ENV, "")).strip()
    if not raw:
        return DEFAULT_MONTHLY_CAP
    if not raw.isdigit():
        raise ValueError(
            "SIRA_DESKTOP_CHAT_MONTHLY_CAP must be an integer"
        )
    value = int(raw)
    if not 1 <= value <= MAX_MONTHLY_CAP:
        raise ValueError(
            "desktop chat monthly cap must be 1..1000"
        )
    return value


def _ledger_path(
    root: Path,
    month: str,
) -> Path:
    return (
        root
        / "runtime"
        / "desktop"
        / "chat_usage"
        / f"{month}.json"
    )


def _read_ledger(
    root: Path,
    month: str,
) -> dict[str, object]:
    path = _ledger_path(root, month)
    empty = {
        "schema": "sira.desktop_chat_usage.v1",
        "month": month,
        "executed_requests": 0,
    }
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 16 * 1024
        ):
            return empty
        value = json.loads(
            path.read_text(encoding="utf-8")
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        return empty
    count = (
        value.get("executed_requests")
        if isinstance(value, dict)
        else None
    )
    if (
        not isinstance(value, dict)
        or value.get("schema")
        != "sira.desktop_chat_usage.v1"
        or value.get("month") != month
        or type(count) is not int
        or count < 0
    ):
        return empty
    return value


def _write_ledger(
    root: Path,
    month: str,
    count: int,
) -> None:
    write_json(
        _ledger_path(root, month),
        {
            "schema": "sira.desktop_chat_usage.v1",
            "month": month,
            "executed_requests": count,
            "paid_spending_authorized": False,
            "billing_changes_authorized": False,
            "note": (
                "Local desktop-chat request counter only. "
                "External/shared provider quota is not observable."
            ),
        },
    )


def desktop_chat_cost_guard(
    root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    root = Path(root).resolve()
    env = os.environ if environ is None else environ
    settings = DesktopChatSettingsStore(root).read()
    confirmed = bool(
        settings.get("gemini_free_tier_confirmed")
    ) or _truthy(env.get(FREE_TIER_CONFIRM_ENV))
    month = _month()
    used = int(
        _read_ledger(root, month)["executed_requests"]
    )
    cap = _cap(env)

    if not confirmed:
        allowed = False
        reason = "free_tier_confirmation_missing"
    elif used >= cap:
        allowed = False
        reason = "desktop_chat_monthly_cap_reached"
    else:
        allowed = True
        reason = "zero_cost_use_owner_confirmed"

    return {
        "schema": "sira.desktop_chat_cost_guard.v1",
        "allowed": allowed,
        "reason": reason,
        "free_tier_confirmed": confirmed,
        "monthly_cap": cap,
        "local_requests_this_month": used,
        "remaining_local_requests": max(0, cap - used),
        "external_quota_observable": False,
        "paid_spending_authorized": False,
        "billing_changes_authorized": False,
    }


def _presence_env(
    root: Path,
    env: Mapping[str, str],
) -> dict[str, str]:
    result = dict(env)
    if load_optional_key(
        root,
        "GEMINI_API_KEY",
    ) is not None:
        result["GEMINI_API_KEY"] = "configured"
    return result


def _broker_preflight(
    root: Path,
    *,
    environ: Mapping[str, str],
) -> dict[str, object]:
    key_configured = (
        load_optional_key(
            root,
            "GEMINI_API_KEY",
        )
        is not None
    )
    # Desktop conversation has its own strict owner-confirmed request cap.
    # The custom health row prevents the autonomous-loop metered cadence
    # from making interactive owner chat unusable, while broker capability
    # and credential classification still remain explicit.
    if key_configured:
        health = {
            "schema": "sira.provider_health.v1",
            "providers": [{
                "provider_id": "gemini",
                "available_now": True,
                "health": "ready",
                "historical_unreliable": False,
                "cooldown_active": False,
                "policy_reason": "desktop_chat_guard_ready",
                "cost_class": "metered",
            }],
        }
    else:
        health = None

    return broker_decision(
        root,
        CAPABILITY,
        allow_metered=True,
        environ=_presence_env(root, environ),
        health_snapshot=health,
        persist=True,
    )


def _create_access_request(
    root: Path,
    preflight: Mapping[str, object],
    *,
    task_id: str,
) -> dict[str, object] | None:
    raw = preflight.get("access_need")
    if not isinstance(raw, Mapping):
        return None
    if raw.get("kind") != KIND_CREDENTIAL:
        return None
    try:
        need = AccessNeed(
            kind=str(raw["kind"]),
            resource=str(raw["resource"]),
            reason=str(raw["reason"]),
            risk=str(raw.get("risk") or RISK_LOW),
            provider_id=(
                str(raw["provider_id"])
                if isinstance(
                    raw.get("provider_id"),
                    str,
                )
                else None
            ),
            credential_name=(
                str(raw["credential_name"])
                if isinstance(
                    raw.get("credential_name"),
                    str,
                )
                else None
            ),
            owner_action=(
                str(raw["owner_action"])
                if isinstance(
                    raw.get("owner_action"),
                    str,
                )
                else None
            ),
        )
    except (
        KeyError,
        TypeError,
        ValueError,
    ):
        return None

    return AccessRequestStore(root).create(
        need,
        task_kind="desktop_chat",
        task_id=task_id,
        resume_hint=(
            "Retry SIRA Desktop chat after GEMINI_API_KEY "
            "is verified locally."
        ),
    )


def _clean_message(
    message: str,
) -> str:
    if not isinstance(message, str):
        raise ValueError("message must be text")
    cleaned = message.strip()
    if not 1 <= len(cleaned) <= MAX_MESSAGE_CHARS:
        raise ValueError(
            f"message must be 1..{MAX_MESSAGE_CHARS} characters"
        )
    cleaned.encode("utf-8")
    return cleaned


def _history(
    rows: list[dict[str, object]] | None,
) -> list[dict[str, str]]:
    if not isinstance(rows, list):
        return []
    result: list[dict[str, str]] = []
    total = 0
    for row in rows[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(row, Mapping):
            continue
        role = row.get("role")
        text = row.get("text")
        if (
            role not in {"user", "assistant"}
            or not isinstance(text, str)
        ):
            continue
        clean = text.strip()
        if not clean:
            continue
        clean = clean[:2000]
        if total + len(clean) > MAX_HISTORY_TEXT_CHARS:
            break
        total += len(clean)
        result.append({
            "role": str(role),
            "text": clean,
        })
    return result


def _memory_context(
    root: Path,
    message: str,
) -> list[dict[str, object]]:
    query = " ".join(message.split())[:300]
    try:
        rows = MemoryStore(root).retrieve(
            query,
            MAX_MEMORY_ROWS,
            autonomous=False,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
    ):
        return []

    result: list[dict[str, object]] = []
    for row in rows[:MAX_MEMORY_ROWS]:
        if not isinstance(row, Mapping):
            continue
        result.append({
            "memory_id": row.get("memory_id"),
            "summary": str(
                row.get("summary") or ""
            )[:MAX_MEMORY_SUMMARY_CHARS],
            "category": row.get("category"),
            "capability": row.get("capability"),
            "status": row.get("status"),
            "confidence": row.get("confidence"),
            "retrieval_score": row.get("retrieval_score"),
        })
    return result


def _runtime_context(
    root: Path,
) -> dict[str, object]:
    status = RuntimeStateStore(root).status()
    last = (
        status.get("last_cycle")
        if isinstance(
            status.get("last_cycle"),
            Mapping,
        )
        else None
    )
    return {
        "desired_state": status.get("desired_state"),
        "effective_state": status.get("effective_state"),
        "worker_state": status.get("worker_state"),
        "worker_alive": status.get("worker_alive"),
        "generation": status.get("generation"),
        "state_health": status.get("state_health"),
        "last_cycle": (
            {
                "status": last.get("status"),
                "target_kind": last.get("target_kind"),
                "outcome": last.get("outcome"),
                "completed_at": last.get("completed_at"),
            }
            if last is not None
            else None
        ),
    }


def _validated_model_result(
    data: Mapping[str, object],
) -> dict[str, object]:
    reply = data.get("reply")
    intent = data.get("intent")
    needs_research = data.get("needs_research")
    if (
        not isinstance(reply, str)
        or not 1 <= len(reply.strip()) <= MAX_REPLY_CHARS
    ):
        raise ValueError("invalid desktop-chat reply")
    if (
        not isinstance(intent, str)
        or not 1 <= len(intent.strip()) <= 120
    ):
        raise ValueError("invalid desktop-chat intent")
    if type(needs_research) is not bool:
        raise ValueError(
            "invalid desktop-chat research flag"
        )
    return {
        "reply": reply.strip(),
        "intent": intent.strip(),
        "needs_research": needs_research,
    }


def desktop_chat_status(
    root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    root = Path(root).resolve()
    env = os.environ if environ is None else environ
    guard = desktop_chat_cost_guard(
        root,
        environ=env,
    )
    preflight = _broker_preflight(
        root,
        environ=env,
    )
    return {
        "schema": "sira.desktop_chat_status.v1",
        "model_backed_chat": True,
        "provider": "gemini",
        "model_id": "gemini-3.1-flash-lite",
        "ready": bool(
            guard.get("allowed")
            and preflight.get("status") == STATUS_READY
        ),
        "cost_guard": guard,
        "capability_broker": {
            "status": preflight.get("status"),
            "selected_provider_id": (
                preflight.get("selected_provider_id")
            ),
            "owner_action_required": bool(
                preflight.get("owner_action_required")
            ),
        },
        "paid_spending_authority": False,
        "billing_changes_authority": False,
        "runtime_control_authority": False,
        "promotion_authority": False,
        "package_install_authority": False,
    }


def run_desktop_model_chat(
    root: Path,
    message: str,
    *,
    history: list[dict[str, object]] | None = None,
    environ: Mapping[str, str] | None = None,
    model: DesktopChatModel | None = None,
) -> dict[str, object]:
    root = Path(root).resolve()
    env = os.environ if environ is None else environ
    message = _clean_message(message)
    request_id = "dc_" + uuid4().hex
    guard = desktop_chat_cost_guard(
        root,
        environ=env,
    )
    preflight = _broker_preflight(
        root,
        environ=env,
    )

    base: dict[str, object] = {
        "schema": "sira.desktop_model_chat.v1",
        "policy_version": CHAT_POLICY_VERSION,
        "request_id": request_id,
        "created_at": utc_now(),
        "status": "blocked",
        "reply": None,
        "intent": None,
        "needs_research": False,
        "cost_guard": guard,
        "capability_broker": {
            "decision_id": preflight.get("decision_id"),
            "status": preflight.get("status"),
            "selected_provider_id": (
                preflight.get("selected_provider_id")
            ),
            "artifact": preflight.get("artifact"),
        },
        "access_request_id": None,
        "metrics": {
            "api_requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "memory_rows": 0,
        },
        "paid_spending": False,
        "billing_changes_performed": False,
        "authority_granted": False,
        "runtime_control_performed": False,
        "promotion_performed": False,
        "package_installation_performed": False,
        "artifact": None,
    }

    if not guard["allowed"]:
        reason = str(guard["reason"])
        base["reply"] = (
            "Model-backed chat is disabled until you explicitly confirm "
            "that this Gemini project is intended for zero-cost/no-charge "
            "use. SIRA will not enable billing or authorize paid spending."
            if reason == "free_tier_confirmation_missing"
            else (
                "The local SIRA Desktop chat request cap for this month "
                "has been reached. Local operational chat remains available."
            )
        )
        return _persist_chat_audit(root, base)

    if preflight.get("status") != STATUS_READY:
        if (
            preflight.get("status")
            == STATUS_CREDENTIAL_REQUIRED
        ):
            access = _create_access_request(
                root,
                preflight,
                task_id=request_id,
            )
            if isinstance(access, Mapping):
                base["access_request_id"] = (
                    access.get("request_id")
                )
            base["reply"] = (
                "Gemini chat needs GEMINI_API_KEY. I created an owner "
                "access request containing metadata only; configure the "
                "credential locally, never paste the secret into chat."
            )
        else:
            base["reply"] = (
                "The model-backed chat provider is currently unavailable "
                "under SIRA's capability policy. Local operational chat "
                "remains available."
            )
        return _persist_chat_audit(root, base)

    context = {
        "message": message,
        "conversation_history": _history(history),
        "runtime": _runtime_context(root),
        "relevant_memories": _memory_context(
            root,
            message,
        ),
        "authority": {
            "runtime_control": False,
            "promotion": False,
            "payment": False,
            "package_install": False,
            "tool_execution": False,
        },
    }
    base["metrics"]["memory_rows"] = len(
        context["relevant_memories"]
    )

    client = model or GeminiDesktopChatModel(
        load_key(root, "GEMINI_API_KEY")
    )

    try:
        batch = client.generate(context)
        validated = _validated_model_result(
            batch.data
        )
    except ProviderError as exc:
        base["status"] = "failed"
        base["reply"] = (
            "The model provider could not complete this message. "
            "No runtime or protected action was performed."
        )
        base["provider_error_code"] = str(
            exc.code
        )[:120]
        base["metrics"]["api_requests"] = max(
            0,
            int(exc.request_count or 0),
        )
        if base["metrics"]["api_requests"]:
            _increment_usage(
                root,
                int(base["metrics"]["api_requests"]),
            )
        return _persist_chat_audit(root, base)
    except (
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
    ):
        base["status"] = "failed"
        base["reply"] = (
            "The model reply failed SIRA's local validation. "
            "No action was performed."
        )
        return _persist_chat_audit(root, base)

    base["status"] = "completed"
    base.update(validated)
    base["metrics"] = {
        **base["metrics"],
        "api_requests": batch.api_requests,
        "input_tokens": batch.input_tokens or 0,
        "output_tokens": batch.output_tokens or 0,
        "attempt_count": batch.attempt_count,
        "retry_delays": list(batch.retry_delays),
    }
    _increment_usage(
        root,
        max(0, int(batch.api_requests)),
    )
    return _persist_chat_audit(root, base)


def _increment_usage(
    root: Path,
    count: int,
) -> None:
    if count <= 0:
        return
    month = _month()
    current = int(
        _read_ledger(root, month)["executed_requests"]
    )
    _write_ledger(
        root,
        month,
        current + count,
    )


def _persist_chat_audit(
    root: Path,
    payload: dict[str, object],
) -> dict[str, object]:
    path = (
        root
        / "runtime"
        / "desktop"
        / "chat_audit"
        / f"{payload['request_id']}.json"
    )
    payload["artifact"] = str(path)
    write_json(path, payload)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return payload
