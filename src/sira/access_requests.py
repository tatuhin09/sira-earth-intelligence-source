from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
from uuid import uuid4

REQUEST_SCHEMA = "sira.access_request.v1"
SNAPSHOT_SCHEMA = "sira.access_requests.v1"

STATUS_PENDING = "pending_owner"
STATUS_APPROVED = "approved_waiting_provision"
STATUS_SATISFIED = "satisfied_resume_ready"
STATUS_DENIED = "denied"
STATUS_CANCELLED = "cancelled"
TERMINAL_STATUSES = {STATUS_DENIED, STATUS_CANCELLED}

KIND_CREDENTIAL = "credential"
KIND_LOGIN = "login"
KIND_PERMISSION = "permission"
KIND_INFORMATION = "information"
KIND_EXTERNAL_ACCOUNT = "external_account"
KIND_SYSTEM_ACCESS = "system_access"
KIND_PAYMENT_APPROVAL = "payment_approval"

ALLOWED_KINDS = {
    KIND_CREDENTIAL, KIND_LOGIN, KIND_PERMISSION, KIND_INFORMATION,
    KIND_EXTERNAL_ACCOUNT, KIND_SYSTEM_ACCESS, KIND_PAYMENT_APPROVAL,
}

RISK_LOW = "low"
RISK_MODERATE = "moderate"
RISK_HIGH = "high"
RISK_FINANCIAL = "financial"
ALLOWED_RISK = {RISK_LOW, RISK_MODERATE, RISK_HIGH, RISK_FINANCIAL}

_REQUEST_ID_RE = re.compile(r"^ar_[0-9a-f]{32}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/@-]{1,128}$")
MAX_REASON = 1200
MAX_NOTE = 800
MAX_REQUEST_FILES = 512
MAX_FILE_BYTES = 64 * 1024

_SENSITIVE_KEYS = {
    "secret", "secret_value", "password", "passphrase", "token", "api_key",
    "key_value", "card_number", "cvv", "private_key", "session_cookie",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: object, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    clean = " ".join(value.strip().split())
    if not clean:
        raise ValueError(f"{field} must not be empty")
    if len(clean) > limit:
        raise ValueError(f"{field} is too long")
    return clean


def _safe_name(value: object, *, field: str) -> str:
    clean = _safe_text(value, field=field, limit=128)
    if not _SAFE_NAME_RE.fullmatch(clean):
        raise ValueError(f"invalid {field}")
    if clean.startswith("/") or any(part in {".", ".."} for part in clean.split("/")):
        raise ValueError(f"invalid {field}")
    return clean


def _validate_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("invalid access request id")
    return request_id


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    data = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
    with open(tmp, "x", encoding="utf-8") as handle:
        os.chmod(tmp, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _assert_no_sensitive_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _SENSITIVE_KEYS:
                raise ValueError("secret-bearing fields are forbidden in access request records")
            _assert_no_sensitive_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_sensitive_keys(child)


@dataclass(frozen=True, slots=True)
class AccessNeed:
    kind: str
    resource: str
    reason: str
    risk: str = RISK_MODERATE
    provider_id: str | None = None
    credential_name: str | None = None
    owner_action: str | None = None

    def validate(self) -> None:
        if self.kind not in ALLOWED_KINDS:
            raise ValueError("unsupported access request kind")
        _safe_name(self.resource, field="resource")
        _safe_text(self.reason, field="reason", limit=MAX_REASON)
        if self.risk not in ALLOWED_RISK:
            raise ValueError("invalid access request risk")
        if self.provider_id is not None:
            _safe_name(self.provider_id, field="provider_id")
        if self.credential_name is not None:
            _safe_name(self.credential_name, field="credential_name")
        if self.owner_action is not None:
            _safe_text(self.owner_action, field="owner_action", limit=MAX_NOTE)
        if self.kind == KIND_CREDENTIAL and self.credential_name is None:
            raise ValueError("credential access request requires credential_name")
        if self.kind == KIND_PAYMENT_APPROVAL and self.risk != RISK_FINANCIAL:
            raise ValueError("payment approval must use financial risk")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return asdict(self)


class AccessRequestStore:
    """Persistent owner-action queue containing metadata only, never secret values."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.base = self.root / "runtime" / "access_requests"
        self.requests_dir = self.base / "requests"

    def _path(self, request_id: str) -> Path:
        return self.requests_dir / f"{_validate_request_id(request_id)}.json"

    @staticmethod
    def _fingerprint(need: AccessNeed, *, task_kind: str | None, task_id: str | None) -> str:
        material = {"need": need.to_dict(), "task_kind": task_kind, "task_id": task_id}
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def create(
        self,
        need: AccessNeed,
        *,
        task_kind: str | None = None,
        task_id: str | None = None,
        resume_hint: str | None = None,
    ) -> dict[str, Any]:
        need.validate()
        if (task_kind is None) != (task_id is None):
            raise ValueError("task_kind and task_id must be provided together")
        if task_kind is not None:
            task_kind = _safe_name(task_kind, field="task_kind")
            task_id = _safe_name(task_id, field="task_id")
        if resume_hint is not None:
            resume_hint = _safe_text(resume_hint, field="resume_hint", limit=MAX_NOTE)

        fingerprint = self._fingerprint(need, task_kind=task_kind, task_id=task_id)
        for existing in self.iter_records():
            if (
                existing.get("fingerprint") == fingerprint
                and existing.get("status") in {STATUS_PENDING, STATUS_APPROVED, STATUS_SATISFIED}
                and existing.get("resume_consumed_at") is None
            ):
                return existing

        request_id = "ar_" + uuid4().hex
        now = _utc_now()
        payload: dict[str, Any] = {
            "schema": REQUEST_SCHEMA,
            "kind": "access_request",
            "request_id": request_id,
            "status": STATUS_PENDING,
            "created_at": now,
            "updated_at": now,
            "fingerprint": fingerprint,
            "need": need.to_dict(),
            "blocked_task": (
                {"task_kind": task_kind, "task_id": task_id, "resume_hint": resume_hint}
                if task_kind is not None else None
            ),
            "owner_action_required": True,
            "continue_other_work": True,
            "notification_required": True,
            "decision_note": None,
            "decided_at": None,
            "satisfied_at": None,
            "resume_consumed_at": None,
            "secret_values_stored": False,
            "authority_note": (
                "This record requests owner action; it is not a credential, bearer token, "
                "payment authorization, or permission bypass."
            ),
        }
        _assert_no_sensitive_keys(payload)
        _atomic_write_json(self._path(request_id), payload)
        return payload

    def load(self, request_id: str) -> dict[str, Any]:
        payload = _safe_read_json(self._path(request_id))
        if (
            payload is None
            or payload.get("schema") != REQUEST_SCHEMA
            or payload.get("kind") != "access_request"
            or payload.get("request_id") != request_id
        ):
            raise ValueError("access request record is unavailable")
        return payload

    def iter_records(self) -> Iterable[dict[str, Any]]:
        if self.requests_dir.is_symlink() or not self.requests_dir.is_dir():
            return ()
        try:
            paths = sorted(self.requests_dir.glob("ar_*.json"))[:MAX_REQUEST_FILES]
        except OSError:
            return ()
        rows: list[dict[str, Any]] = []
        for path in paths:
            payload = _safe_read_json(path)
            if (
                payload is not None
                and payload.get("schema") == REQUEST_SCHEMA
                and payload.get("kind") == "access_request"
                and isinstance(payload.get("request_id"), str)
            ):
                rows.append(payload)
        return tuple(rows)

    def _update(self, request_id: str, **fields: Any) -> dict[str, Any]:
        payload = self.load(request_id)
        payload.update(fields)
        payload["updated_at"] = _utc_now()
        _assert_no_sensitive_keys(payload)
        _atomic_write_json(self._path(request_id), payload)
        return payload

    def approve(self, request_id: str, *, note: str | None = None) -> dict[str, Any]:
        payload = self.load(request_id)
        if payload.get("status") != STATUS_PENDING:
            raise ValueError("only pending access requests can be approved")
        clean_note = _safe_text(note, field="decision_note", limit=MAX_NOTE) if note is not None else None
        return self._update(
            request_id,
            status=STATUS_APPROVED,
            decision_note=clean_note,
            decided_at=_utc_now(),
            notification_required=False,
        )

    def deny(self, request_id: str, *, note: str | None = None) -> dict[str, Any]:
        payload = self.load(request_id)
        if payload.get("status") not in {STATUS_PENDING, STATUS_APPROVED}:
            raise ValueError("access request cannot be denied from its current state")
        clean_note = _safe_text(note, field="decision_note", limit=MAX_NOTE) if note is not None else None
        return self._update(
            request_id,
            status=STATUS_DENIED,
            decision_note=clean_note,
            decided_at=_utc_now(),
            notification_required=False,
        )

    def cancel(self, request_id: str, *, note: str | None = None) -> dict[str, Any]:
        payload = self.load(request_id)
        if payload.get("status") in TERMINAL_STATUSES:
            return payload
        clean_note = (
            _safe_text(note, field="decision_note", limit=MAX_NOTE)
            if note is not None else payload.get("decision_note")
        )
        return self._update(
            request_id,
            status=STATUS_CANCELLED,
            decision_note=clean_note,
            decided_at=payload.get("decided_at") or _utc_now(),
            notification_required=False,
        )

    def mark_satisfied(self, request_id: str) -> dict[str, Any]:
        payload = self.load(request_id)
        if payload.get("status") != STATUS_APPROVED:
            raise ValueError("access request must be owner-approved before satisfaction")
        need = payload.get("need")
        if isinstance(need, Mapping) and need.get("kind") == KIND_PAYMENT_APPROVAL:
            raise ValueError(
                "payment approvals are one-shot owner decisions and cannot be converted "
                "into standing autonomous authority"
            )
        return self._update(
            request_id,
            status=STATUS_SATISFIED,
            satisfied_at=_utc_now(),
            notification_required=False,
        )

    def resume_ready(self) -> tuple[dict[str, Any], ...]:
        rows = [
            row for row in self.iter_records()
            if (
                row.get("status") == STATUS_SATISFIED
                and row.get("resume_consumed_at") is None
                and isinstance(row.get("blocked_task"), Mapping)
            )
        ]
        rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("request_id") or "")))
        return tuple(rows)

    def consume_resume(self, request_id: str) -> dict[str, Any]:
        payload = self.load(request_id)
        if payload.get("status") != STATUS_SATISFIED:
            raise ValueError("only satisfied requests can release parked work")
        if not isinstance(payload.get("blocked_task"), Mapping):
            raise ValueError("access request has no parked task")
        if payload.get("resume_consumed_at") is not None:
            return payload
        return self._update(request_id, resume_consumed_at=_utc_now())

    def pending_notifications(self) -> tuple[dict[str, object], ...]:
        rows: list[dict[str, object]] = []
        for payload in self.iter_records():
            if payload.get("status") != STATUS_PENDING or payload.get("notification_required") is not True:
                continue
            need = payload.get("need")
            if not isinstance(need, Mapping):
                continue
            rows.append({
                "request_id": payload.get("request_id"),
                "kind": need.get("kind"),
                "resource": need.get("resource"),
                "reason": need.get("reason"),
                "risk": need.get("risk"),
                "provider_id": need.get("provider_id"),
                "credential_name": need.get("credential_name"),
                "owner_action": need.get("owner_action"),
                "blocked_task": payload.get("blocked_task"),
                "continue_other_work": True,
                "contains_secret_value": False,
            })
        rows.sort(key=lambda row: str(row.get("request_id") or ""))
        return tuple(rows)

    def snapshot(self) -> dict[str, object]:
        rows = list(self.iter_records())
        return {
            "schema": SNAPSHOT_SCHEMA,
            "read_only_view": True,
            "request_count": len(rows),
            "pending_count": sum(1 for row in rows if row.get("status") == STATUS_PENDING),
            "approved_count": sum(1 for row in rows if row.get("status") == STATUS_APPROVED),
            "resume_ready_count": len(self.resume_ready()),
            "pending_notification_count": len(self.pending_notifications()),
            "requests": rows,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local SIRA owner access-request queue.")
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list")
    sub.add_parser("notifications")
    sub.add_parser("resume-ready")

    show = sub.add_parser("show")
    show.add_argument("request_id")

    create = sub.add_parser("create")
    create.add_argument("--kind", required=True, choices=sorted(ALLOWED_KINDS))
    create.add_argument("--resource", required=True)
    create.add_argument("--reason", required=True)
    create.add_argument("--risk", choices=sorted(ALLOWED_RISK), default=RISK_MODERATE)
    create.add_argument("--provider")
    create.add_argument("--credential-name")
    create.add_argument("--owner-action")
    create.add_argument("--task-kind")
    create.add_argument("--task-id")
    create.add_argument("--resume-hint")

    for name in ("approve", "deny", "cancel"):
        cmd = sub.add_parser(name)
        cmd.add_argument("request_id")
        cmd.add_argument("--note")

    satisfy = sub.add_parser("satisfy")
    satisfy.add_argument("request_id")

    consume = sub.add_parser("consume-resume")
    consume.add_argument("request_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = AccessRequestStore(Path(args.root))

    if args.command == "list":
        result: object = store.snapshot()
    elif args.command == "notifications":
        result = {"schema": "sira.access_notifications.v1", "notifications": list(store.pending_notifications())}
    elif args.command == "resume-ready":
        result = {"schema": "sira.access_resume_ready.v1", "requests": list(store.resume_ready())}
    elif args.command == "show":
        result = store.load(args.request_id)
    elif args.command == "create":
        need = AccessNeed(
            kind=args.kind,
            resource=args.resource,
            reason=args.reason,
            risk=args.risk,
            provider_id=args.provider,
            credential_name=args.credential_name,
            owner_action=args.owner_action,
        )
        result = store.create(
            need,
            task_kind=args.task_kind,
            task_id=args.task_id,
            resume_hint=args.resume_hint,
        )
    elif args.command == "approve":
        result = store.approve(args.request_id, note=args.note)
    elif args.command == "deny":
        result = store.deny(args.request_id, note=args.note)
    elif args.command == "cancel":
        result = store.cancel(args.request_id, note=args.note)
    elif args.command == "satisfy":
        result = store.mark_satisfied(args.request_id)
    elif args.command == "consume-resume":
        result = store.consume_resume(args.request_id)
    else:
        raise AssertionError("unreachable")

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
