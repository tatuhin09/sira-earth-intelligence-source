from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

if __package__ in {None, ""}:
    ROOT = Path(__file__).resolve().parents[2]
    SRC = ROOT / "src"
    SRC_TEXT = str(SRC)
    sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]
    __package__ = "sira"

from .access_requests import AccessRequestStore
from .knowledge_consolidation import KnowledgeConsolidationStore


EVENT_SCHEMA = "sira.owner_notification.v1"
SNAPSHOT_SCHEMA = "sira.owner_notifications.v1"

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_CANCELLED = "cancelled_source_resolved"

MAX_EVENTS = 512
MAX_FILE_BYTES = 64 * 1024
MAX_TITLE = 120
MAX_BODY = 1200
BASE_RETRY_SECONDS = 30
MAX_RETRY_SECONDS = 3600

_NOTIFICATION_ID_RE = re.compile(r"^nt_[0-9a-f]{32}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    clean = " ".join(value.strip().split())
    return clean[:limit]


def _safe_read(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with open(tmp, "x", encoding="utf-8") as handle:
        os.chmod(tmp, 0o600)
        json.dump(dict(payload), handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _retry_delay(attempt_count: int) -> int:
    exponent = max(0, min(int(attempt_count) - 1, 7))
    return min(MAX_RETRY_SECONDS, BASE_RETRY_SECONDS * (2 ** exponent))


def _notification_text(notice: Mapping[str, object]) -> tuple[str, str, str]:
    resource = _safe_text(notice.get("resource"), limit=80) or "external access"
    reason = _safe_text(notice.get("reason"), limit=500) or "Required access is unavailable."
    action = _safe_text(notice.get("owner_action"), limit=500)
    risk = _safe_text(notice.get("risk"), limit=40) or "moderate"

    title = _safe_text(f"SIRA needs owner access: {resource}", limit=MAX_TITLE)
    body = f"{reason}"
    if action:
        body += f" Action: {action}"
    body += " SIRA will continue other safe work while this task is parked."
    body = _safe_text(body, limit=MAX_BODY)
    urgency = "critical" if risk in {"high", "financial"} else "normal"
    return title, body, urgency


class OwnerNotificationStore:
    """Durable local owner-notification outbox. No secret values are accepted."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.base = self.root / "runtime" / "owner_notifications"
        self.events_dir = self.base / "events"

    def _path(self, notification_id: str) -> Path:
        if not isinstance(notification_id, str) or not _NOTIFICATION_ID_RE.fullmatch(notification_id):
            raise ValueError("invalid owner notification id")
        return self.events_dir / f"{notification_id}.json"

    def iter_events(self) -> tuple[dict[str, Any], ...]:
        if self.events_dir.is_symlink() or not self.events_dir.is_dir():
            return ()
        try:
            paths = sorted(self.events_dir.glob("nt_*.json"))[:MAX_EVENTS]
        except OSError:
            return ()
        rows: list[dict[str, Any]] = []
        for path in paths:
            row = _safe_read(path)
            if (
                row is not None
                and row.get("schema") == EVENT_SCHEMA
                and row.get("kind") == "owner_notification"
                and _NOTIFICATION_ID_RE.fullmatch(path.stem)
                and row.get("notification_id") == path.stem
            ):
                rows.append(row)
        rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("notification_id") or "")))
        return tuple(rows)

    def load(self, notification_id: str) -> dict[str, Any]:
        row = _safe_read(self._path(notification_id))
        if (
            row is None
            or row.get("schema") != EVENT_SCHEMA
            or row.get("kind") != "owner_notification"
            or row.get("notification_id") != notification_id
        ):
            raise ValueError("owner notification is unavailable")
        return row

    def _save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(payload)
        row["updated_at"] = _utc_now()
        _atomic_write(self._path(str(row["notification_id"])), row)
        return row

    def find_source(self, source_request_id: str) -> dict[str, Any] | None:
        for row in self.iter_events():
            if (
                row.get("source_kind") == "access_request"
                and row.get("source_request_id") == source_request_id
            ):
                return row
        return None

    def enqueue_access_notice(self, notice: Mapping[str, object]) -> dict[str, Any]:
        request_id = notice.get("request_id")
        if not isinstance(request_id, str) or not request_id.startswith("ar_"):
            raise ValueError("access notice requires a request id")
        existing = self.find_source(request_id)
        if existing is not None:
            return existing

        title, body, urgency = _notification_text(notice)
        now = _utc_now()
        payload: dict[str, Any] = {
            "schema": EVENT_SCHEMA,
            "kind": "owner_notification",
            "notification_id": "nt_" + uuid4().hex,
            "source_kind": "access_request",
            "source_request_id": request_id,
            "category": "owner_access_required",
            "status": STATUS_PENDING,
            "created_at": now,
            "updated_at": now,
            "title": title,
            "body": body,
            "urgency": urgency,
            "transport": "desktop_notify_send",
            "attempt_count": 0,
            "last_attempt_at": None,
            "next_attempt_epoch": 0.0,
            "last_error_code": None,
            "delivered_at": None,
            "acknowledged_at": None,
            "cancelled_at": None,
            "contains_secret_value": False,
        }
        _atomic_write(self._path(payload["notification_id"]), payload)
        return payload

    def enqueue_learning_result(self, goal: Mapping[str, object],
                                report: Mapping[str, object]) -> list[dict[str, Any]]:
        """Queue local notices for confirmed new claims or a persistent study gap."""
        goal_id = goal.get("goal_id")
        topic = goal.get("topic")
        if (not isinstance(goal_id, str) or not re.fullmatch(r"lg_[0-9a-f]{32}", goal_id)
                or not isinstance(topic, str) or not 1 <= len(topic) <= 500
                or report.get("learning_goal_id") != goal_id or report.get("topic") != topic):
            raise ValueError("Learning notice goal and report must match")
        if self.events_dir.is_symlink():
            raise ValueError("Learning notification directory is unsafe")

        notices: list[tuple[str, str, str, str]] = []
        if report.get("outcome") == "verified_knowledge_recorded":
            review = report.get("document_review")
            claims = review.get("claims") if isinstance(review, Mapping) else None
            if isinstance(claims, list) and type(report.get("verified_claim_count")) is int:
                memory = KnowledgeConsolidationStore(self.root)
                for item in claims[:3]:
                    if not isinstance(item, Mapping):
                        continue
                    key, claim = item.get("knowledge_key"), item.get("claim")
                    if (not isinstance(key, str) or not re.fullmatch(r"claim\.[0-9a-f]{32}", key)
                            or not isinstance(claim, str) or not 1 <= len(claim) <= 360):
                        continue
                    known = memory.lookup(key)
                    if (known is None or known.get("status") != "active"
                            or known.get("freshness") != "fresh"
                            or known.get("claim_text") != claim
                            or known.get("host_count", 0) < 2):
                        continue
                    notices.append((key, "learning_verified_claim",
                                    f"SIRA verified a claim for {topic}",
                                    f"Verified from two independent public hosts: {claim} "
                                    "See Goals show for study progress."))

        step = report.get("study_step_index")
        if (not notices and report.get("outcome") in {
                "independent_source_unavailable", "research_sources_insufficient", "research_failed"}
                and type(step) is int and report.get("verified_claim_count") == 0):
            plan, stats = goal.get("study_plan"), goal.get("study_step_stats")
            if (isinstance(plan, list) and isinstance(stats, list)
                    and 0 <= step < len(plan) == len(stats)
                    and isinstance(stats[step], Mapping)
                    and stats[step].get("attempt_count", 0) >= 2
                    and stats[step].get("verified_claim_events") == 0):
                focus = str(plan[step])
                notices.append((f"gap:{step}", "learning_evidence_gap",
                                f"SIRA needs more evidence for {topic}",
                                f"Repeated study of {focus} has no verified claim yet. "
                                "SIRA will revisit it; see Goals show for progress."))

        created: list[dict[str, Any]] = []
        for suffix, category, title, body in notices:
            source_id = f"{goal_id}:{suffix}"
            notification_id = "nt_" + hashlib.sha256(
                f"learning_goal:{source_id}".encode("utf-8")
            ).hexdigest()[:32]
            path = self._path(notification_id)
            if path.exists() or path.is_symlink():
                existing = self.load(notification_id)
                if (existing.get("source_kind") != "learning_goal"
                        or existing.get("source_request_id") != source_id):
                    raise ValueError("Learning notification identity mismatch")
                continue
            if len(self.iter_events()) >= MAX_EVENTS:
                break
            now = _utc_now()
            row: dict[str, Any] = {
                "schema": EVENT_SCHEMA, "kind": "owner_notification",
                "notification_id": notification_id,
                "source_kind": "learning_goal", "source_request_id": source_id,
                "category": category, "status": STATUS_PENDING,
                "created_at": now, "updated_at": now,
                "title": _safe_text(title, limit=MAX_TITLE),
                "body": _safe_text(body, limit=MAX_BODY), "urgency": "normal",
                "transport": "desktop_notify_send", "attempt_count": 0,
                "last_attempt_at": None, "next_attempt_epoch": 0.0,
                "last_error_code": None, "delivered_at": None,
                "acknowledged_at": None, "cancelled_at": None,
                "contains_secret_value": False,
            }
            _atomic_write(path, row)
            created.append(row)
        return created

    def reconcile_access_requests(self) -> dict[str, int]:
        notices = AccessRequestStore(self.root).pending_notifications()
        active_ids = {
            str(row.get("request_id"))
            for row in notices
            if isinstance(row.get("request_id"), str)
        }
        created = 0
        cancelled = 0

        for notice in notices:
            request_id = notice.get("request_id")
            if isinstance(request_id, str) and self.find_source(request_id) is None:
                self.enqueue_access_notice(notice)
                created += 1

        for row in self.iter_events():
            if (
                row.get("source_kind") == "access_request"
                and row.get("status") == STATUS_PENDING
                and row.get("source_request_id") not in active_ids
            ):
                row["status"] = STATUS_CANCELLED
                row["cancelled_at"] = _utc_now()
                self._save(row)
                cancelled += 1

        return {"created": created, "cancelled": cancelled}

    def acknowledge(self, notification_id: str) -> dict[str, Any]:
        row = self.load(notification_id)
        if row.get("status") == STATUS_ACKNOWLEDGED:
            return row
        if row.get("status") != STATUS_DELIVERED:
            raise ValueError("only delivered notifications can be acknowledged")
        row["status"] = STATUS_ACKNOWLEDGED
        row["acknowledged_at"] = _utc_now()
        return self._save(row)

    def mark_delivery(
        self,
        notification_id: str,
        *,
        ok: bool,
        now_epoch: float,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        row = self.load(notification_id)
        if row.get("status") != STATUS_PENDING:
            return row
        attempts = int(row.get("attempt_count") or 0) + 1
        row["attempt_count"] = attempts
        row["last_attempt_at"] = _utc_now()

        if ok:
            row["status"] = STATUS_DELIVERED
            row["delivered_at"] = _utc_now()
            row["next_attempt_epoch"] = 0.0
            row["last_error_code"] = None
        else:
            row["last_error_code"] = _safe_text(error_code, limit=80) or "delivery_failed"
            row["next_attempt_epoch"] = float(now_epoch) + _retry_delay(attempts)
        return self._save(row)

    def snapshot(self) -> dict[str, object]:
        rows = list(self.iter_events())
        return {
            "schema": SNAPSHOT_SCHEMA,
            "event_count": len(rows),
            "pending_count": sum(1 for row in rows if row.get("status") == STATUS_PENDING),
            "delivered_count": sum(1 for row in rows if row.get("status") == STATUS_DELIVERED),
            "acknowledged_count": sum(1 for row in rows if row.get("status") == STATUS_ACKNOWLEDGED),
            "cancelled_count": sum(1 for row in rows if row.get("status") == STATUS_CANCELLED),
            "events": rows,
        }


def send_desktop_notification(event: Mapping[str, Any]) -> dict[str, object]:
    """Best-effort local desktop delivery. Never uses a shell."""
    binary = shutil.which("notify-send")
    if binary is None:
        return {"ok": False, "error_code": "notify_send_unavailable"}
    if not os.environ.get("DISPLAY") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return {"ok": False, "error_code": "desktop_session_unavailable"}

    title = _safe_text(event.get("title"), limit=MAX_TITLE)
    body = _safe_text(event.get("body"), limit=MAX_BODY)
    urgency = event.get("urgency")
    if urgency not in {"low", "normal", "critical"}:
        urgency = "normal"

    try:
        proc = subprocess.run(
            [
                binary,
                "--app-name=SIRA",
                f"--urgency={urgency}",
                title,
                body,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "error_code": "notify_send_execution_failed"}

    return {
        "ok": proc.returncode == 0,
        "error_code": None if proc.returncode == 0 else "notify_send_nonzero",
    }


def pump_owner_notifications(
    root: Path,
    *,
    sender: Callable[[Mapping[str, Any]], Mapping[str, object] | bool] = send_desktop_notification,
    now_epoch: float | None = None,
) -> dict[str, object]:
    """Sync access requests into the outbox and deliver eligible pending events."""
    when = time.time() if now_epoch is None else now_epoch
    if not isinstance(when, (int, float)) or isinstance(when, bool) or when < 0:
        raise ValueError("now_epoch must be a non-negative number")

    store = OwnerNotificationStore(root)
    reconciliation = store.reconcile_access_requests()
    attempted = 0
    delivered = 0
    deferred_retry = 0

    for row in store.iter_events():
        if row.get("status") != STATUS_PENDING:
            continue
        next_attempt = row.get("next_attempt_epoch")
        if isinstance(next_attempt, (int, float)) and not isinstance(next_attempt, bool):
            if float(next_attempt) > float(when):
                deferred_retry += 1
                continue

        attempted += 1
        try:
            response = sender(row)
            if isinstance(response, Mapping):
                ok = response.get("ok") is True
                code = response.get("error_code")
                error_code = str(code) if isinstance(code, str) else None
            else:
                ok = response is True
                error_code = None if ok else "delivery_failed"
        except Exception as exc:
            ok = False
            error_code = f"sender_{type(exc).__name__}"

        updated = store.mark_delivery(
            str(row["notification_id"]),
            ok=ok,
            error_code=error_code,
            now_epoch=float(when),
        )
        delivered += int(updated.get("status") == STATUS_DELIVERED)

    snapshot = store.snapshot()
    return {
        "schema": "sira.owner_notification_pump.v1",
        "reconciliation": reconciliation,
        "attempted": attempted,
        "delivered": delivered,
        "deferred_retry": deferred_retry,
        "pending_count": snapshot["pending_count"],
        "delivered_count": snapshot["delivered_count"],
        "acknowledged_count": snapshot["acknowledged_count"],
        "cancelled_count": snapshot["cancelled_count"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SIRA local owner notification outbox.")
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("deliver")
    ack = sub.add_parser("ack")
    ack.add_argument("notification_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root)
    store = OwnerNotificationStore(root)

    if args.command == "list":
        store.reconcile_access_requests()
        result: object = store.snapshot()
    elif args.command == "deliver":
        result = pump_owner_notifications(root)
    elif args.command == "ack":
        result = store.acknowledge(args.notification_id)
    else:
        raise AssertionError("unreachable")

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
