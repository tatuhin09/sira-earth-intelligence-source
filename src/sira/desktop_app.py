"""Protected localhost-only desktop control bridge for SIRA Desktop v0.1.

This module exposes a very small HTTP surface bound to 127.0.0.1 only.
Read operations are available to the same-origin UI. State-changing POST
operations require an unpredictable per-process session token in a custom
header, which prevents cross-origin form/CSRF control of SIRA.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlparse
import webbrowser

from .models import utc_now
from .owner_notifications import OwnerNotificationStore, pump_owner_notifications
from .runtime import AutonomousRuntime, RuntimeStateStore
from .runtime_release import run_release_acceptance
from .desktop_chat import (
    DesktopChatSettingsStore,
    desktop_chat_status,
    run_desktop_model_chat,
)
from .desktop_research import (
    DesktopResearchJobStore,
    DesktopResearchSettingsStore,
    explicit_research_requested,
    research_settings_status,
    recover_interrupted_research_jobs,
    run_research_job,
)
from .desktop_research_memory import (
    ingest_verified_desktop_research,
    reconcile_completed_desktop_research,
)
from .desktop_verified_knowledge import verified_knowledge_reply
from .desktop_verified_knowledge import _terms as _knowledge_terms
from .desktop_learning_goals import learning_goal_chat_reply
from .desktop_core_view import compact_core_state, operational_text, verified_memory_text
from .sira_core import SiraCore
from .storage import write_json

DESKTOP_POLICY_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 16 * 1024
MAX_CHAT_CHARS = 4000
MAX_CHAT_HISTORY = 200
MAX_ACTIVITY_ITEMS = 60
MAX_JSON_ARTIFACT_BYTES = 2 * 1024 * 1024


def _matching_pending_evidence(root: Path, message: str) -> bool:
    """Keep established guarded conversation for a matching weak local draft.

    A pending source is never presented as verified memory. This check reads at
    most 16 local claim texts and gives no new model/provider permission.
    """
    db = root / "memory" / "sira_knowledge.sqlite3"
    if db.is_symlink() or not db.is_file() or db.stat().st_size > 64 * 1024 * 1024:
        return False
    terms = _knowledge_terms(message)
    if len(terms) < 2:
        return False
    try:
        from urllib.parse import quote
        uri = "file:" + quote(str(db.resolve()), safe="/") + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            rows = conn.execute(
                "SELECT claim_text FROM knowledge_evidence ORDER BY retrieved_at DESC LIMIT 16"
            ).fetchall()
        return any(len(terms & _knowledge_terms(str(row[0]))) >= 2 and
                   len(terms & _knowledge_terms(str(row[0]))) / len(terms) >= .5
                   for row in rows)
    except (sqlite3.DatabaseError, OSError, ValueError):
        return False


def _safe_json_file(path: Path) -> dict[str, Any] | None:
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > MAX_JSON_ARTIFACT_BYTES
        ):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _latest_json(directory: Path, pattern: str) -> dict[str, Any] | None:
    try:
        paths = [
            path
            for path in directory.glob(pattern)
            if path.is_file() and not path.is_symlink()
        ]
        paths.sort(
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
    except OSError:
        return None
    for path in paths:
        value = _safe_json_file(path)
        if value is not None:
            value = dict(value)
            value["_path"] = str(path)
            return value
    return None


def _git_summary(root: Path) -> dict[str, object]:
    revision = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if revision.returncode != 0 or status.returncode != 0:
        return {
            "available": False,
            "revision": None,
            "clean": False,
        }
    return {
        "available": True,
        "revision": revision.stdout.strip()[:80],
        "clean": not bool(status.stdout.strip()),
    }


def _memory_summary(root: Path) -> dict[str, object]:
    db = root / "memory" / "sira_memory.sqlite3"
    if db.is_symlink() or not db.is_file():
        return {
            "healthy": False,
            "schema_version": None,
            "counts": {
                "memories": 0,
                "occurrences": 0,
                "transitions": 0,
            },
        }
    try:
        uri = f"file:{db}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as conn:
            quick = conn.execute("PRAGMA quick_check").fetchone()
            quick_check = str(quick[0]) if quick else "unknown"
            metadata = conn.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            schema_version = (
                int(metadata[0]) if metadata is not None else None
            )
            counts = {}
            for table in ("memories", "occurrences", "transitions"):
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()
                counts[table] = int(row[0]) if row else 0
    except (sqlite3.DatabaseError, OSError, ValueError):
        return {
            "healthy": False,
            "schema_version": None,
            "counts": {
                "memories": 0,
                "occurrences": 0,
                "transitions": 0,
            },
        }
    return {
        "healthy": quick_check == "ok",
        "quick_check": quick_check,
        "schema_version": schema_version,
        "counts": counts,
    }


def _notification_summary(root: Path) -> dict[str, object]:
    try:
        events = OwnerNotificationStore(root).iter_events()
    except (OSError, ValueError):
        return {
            "readable": False,
            "total": 0,
            "by_status": {},
            "events": [],
            "latest": None,
            "truncated": False,
        }
    counts: dict[str, int] = {}
    safe_events: list[dict[str, object]] = []
    secret_assignment = re.compile(
        r"(?i)\b([a-z][a-z0-9_]*(?:api[_-]?key|token|secret|password|credential)[a-z0-9_]*)\s*[:=]\s*([^\s&;,]+)"
    )
    bearer = re.compile(r"(?i)\b(Bearer)\s+[^\s&;,]+")
    query_secret = re.compile(r"(?i)([?&](?:key|token|secret|password|api_key)=)[^&#\s]+")
    def safe_text(value: object, limit: int) -> str:
        if not isinstance(value, str):
            return ""
        clean = " ".join(value.split())[:limit]
        clean = secret_assignment.sub(r"\1=[redacted]", clean)
        clean = bearer.sub(r"\1 [redacted]", clean)
        return query_secret.sub(r"\1[redacted]", clean)

    for event in events[-50:]:
        if event.get("contains_secret_value") is not False:
            continue
        status = event.get("status")
        if not isinstance(status, str) or status not in {"pending", "delivered", "acknowledged", "cancelled_source_resolved"}:
            continue
        source_id = event.get("source_request_id")
        if not isinstance(source_id, str) or not re.fullmatch(r"(?:ar_[0-9a-f]{32}|lg_[0-9a-f]{32}:[a-zA-Z0-9_.:-]{1,128})", source_id):
            source_id = None
        counts[status] = counts.get(status, 0) + 1
        safe_events.append({
            "notification_id": event.get("notification_id"),
            "status": status,
            "title": safe_text(event.get("title"), 120),
            "body": safe_text(event.get("body"), 1200),
            "urgency": event.get("urgency") if isinstance(event.get("urgency"), str) and event.get("urgency") in {"normal", "critical"} else None,
            "category": safe_text(event.get("category"), 80),
            "source_kind": event.get("source_kind") if isinstance(event.get("source_kind"), str) and event.get("source_kind") in {"access_request", "learning_goal"} else None,
            "source_request_id": source_id,
            "created_at": safe_text(event.get("created_at"), 48),
            "delivered_at": safe_text(event.get("delivered_at"), 48),
        })
    return {
        "readable": True,
        "total": len(safe_events),
        "by_status": dict(sorted(counts.items())),
        "events": safe_events,
        "latest": safe_events[-1] if safe_events else None,
        "truncated": len(events) > 50,
    }


def _activity(root: Path) -> list[dict[str, object]]:
    candidates: list[tuple[int, Path, str]] = []
    sources = (
        (root / "runtime" / "release", "release_acceptance_*.json", "release"),
        (root / "runtime" / "soak", "bounded_soak_*.json", "soak"),
        (root / "runtime" / "checks", "*.json", "diagnostic"),
        (root / "runtime" / "recovery", "*.json", "recovery"),
        (root / "runtime" / "promotions", "**/*.json", "promotion"),
    )
    for directory, pattern, kind in sources:
        try:
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in directory.glob(pattern):
                if path.is_file() and not path.is_symlink():
                    candidates.append(
                        (path.stat().st_mtime_ns, path, kind)
                    )
        except OSError:
            continue

    last_cycle = root / "runtime" / "last_cycle.json"
    if last_cycle.is_file() and not last_cycle.is_symlink():
        try:
            candidates.append(
                (last_cycle.stat().st_mtime_ns, last_cycle, "cycle")
            )
        except OSError:
            pass

    candidates.sort(
        key=lambda item: (item[0], item[1].name),
        reverse=True,
    )
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for _, path, kind in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        value = _safe_json_file(path)
        if value is None:
            continue
        rows.append({
            "kind": kind,
            "status": value.get("status"),
            "outcome": value.get("outcome"),
            "decision_code": value.get("decision_code"),
            "created_at": (
                value.get("created_at")
                or value.get("completed_at")
                or value.get("updated_at")
            ),
            "title": (
                value.get("kind")
                or value.get("schema")
                or path.stem
            ),
            "artifact": str(path),
        })
        if len(rows) >= MAX_ACTIVITY_ITEMS:
            break
    return rows


class DesktopChatStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = (
            self.root / "runtime" / "desktop" / "chat_history.json"
        )

    def load(self) -> list[dict[str, object]]:
        value = _safe_json_file(self.path)
        if value is None:
            return []
        rows = value.get("messages")
        if not isinstance(rows, list):
            return []
        safe: list[dict[str, object]] = []
        for row in rows[-MAX_CHAT_HISTORY:]:
            if not isinstance(row, Mapping):
                continue
            role = row.get("role")
            text = row.get("text")
            if (
                role in {"user", "assistant"}
                and isinstance(text, str)
                and len(text) <= MAX_CHAT_CHARS
            ):
                safe.append({
                    "role": role,
                    "text": text,
                    "created_at": row.get("created_at"),
                })
        return safe

    def append(self, role: str, text: str) -> dict[str, object]:
        if role not in {"user", "assistant"}:
            raise ValueError("invalid chat role")
        if not isinstance(text, str):
            raise ValueError("chat text must be a string")
        cleaned = text.strip()
        if not 1 <= len(cleaned) <= MAX_CHAT_CHARS:
            raise ValueError(
                f"chat text must be 1..{MAX_CHAT_CHARS} characters"
            )
        rows = self.load()
        item = {
            "role": role,
            "text": cleaned,
            "created_at": utc_now(),
        }
        rows.append(item)
        payload = {
            "schema": "sira.desktop_chat.v1",
            "messages": rows[-MAX_CHAT_HISTORY:],
        }
        write_json(self.path, payload)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return item


@dataclass
class DesktopControl:
    root: Path

    def __post_init__(self):
        self.root = Path(self.root).resolve()
        self.core = SiraCore(self.root)
        self.chat = DesktopChatStore(self.root)
        self.research_jobs = DesktopResearchJobStore(self.root)
        self._chat_lock = threading.Lock()
        self._research_lock = threading.Lock()
        self.research_recovery = recover_interrupted_research_jobs(
            self.root
        )
        self.core.reconcile_research_jobs()
        self.research_memory_reconciliation = reconcile_completed_desktop_research(
            self.root
        )

    def overview(self) -> dict[str, object]:
        core = compact_core_state(self.root, self.core.snapshot())
        runtime = core["runtime"]
        release = core["release"]
        memory = _memory_summary(self.root)
        notifications = _notification_summary(self.root)
        git = _git_summary(self.root)
        head = core["identity"].get("head")
        same_head = bool(head and git.get("revision") == head)
        if head and not same_head:
            git["clean"] = False
        ready = release.get("release_ready") is True and same_head and git.get("clean") is True
        return {
            "schema": "sira.desktop_overview.v1",
            "created_at": utc_now(),
            "runtime": runtime,
            "release": {
                "status": "passed" if ready else "unavailable",
                "decision_code": release.get("reason"),
                "release_ready": ready,
                "checks_passed": release.get("required_checks_passed"),
                "checks_required": release.get("required_check_count"),
                "revision": head if ready else None,
            },
            "memory": memory,
            "notifications": {
                "readable": notifications["readable"],
                "total": notifications["total"],
                "by_status": notifications["by_status"],
                "latest": notifications["latest"],
                "truncated": notifications["truncated"],
            },
            "git": git,
            "activity": _activity(self.root)[:12],
            "core": core,
        }

    def runtime_start(self) -> dict[str, object]:
        return AutonomousRuntime(self.root).start()

    def runtime_stop(self) -> dict[str, object]:
        return AutonomousRuntime(self.root).stop()

    def refresh_release(self) -> dict[str, object]:
        return run_release_acceptance(self.root)

    def notifications(self) -> dict[str, object]:
        return _notification_summary(self.root)

    def deliver_notifications(self) -> dict[str, object]:
        return pump_owner_notifications(self.root)

    def activity(self) -> dict[str, object]:
        return {
            "schema": "sira.desktop_activity.v1",
            "items": _activity(self.root),
        }

    def chat_history(self) -> dict[str, object]:
        return {
            "schema": "sira.desktop_chat.v1",
            "messages": self.chat.load(),
        }

    def chat_model_status(self) -> dict[str, object]:
        return desktop_chat_status(self.root)

    def update_chat_settings(
        self,
        free_tier_confirmed: bool,
    ) -> dict[str, object]:
        settings = DesktopChatSettingsStore(
            self.root
        ).set_free_tier_confirmed(
            free_tier_confirmed
        )
        return {
            "schema": "sira.desktop_chat_settings_result.v1",
            "settings": settings,
            "status": desktop_chat_status(self.root),
        }

    def research_status(self) -> dict[str, object]:
        return {
            **research_settings_status(self.root),
            "jobs": self.research_jobs.list(),
            "recovery": dict(self.research_recovery),
        }

    def update_research_settings(
        self,
        search_zero_cost_confirmed: bool,
    ) -> dict[str, object]:
        settings = DesktopResearchSettingsStore(
            self.root
        ).set_search_zero_cost_confirmed(
            search_zero_cost_confirmed
        )
        return {
            "schema": "sira.desktop_research_settings_result.v1",
            "settings": settings,
            "status": self.research_status(),
        }

    def _active_research_job(self) -> dict[str, object] | None:
        for row in self.research_jobs.list():
            if row.get("status") in {"queued", "running"}:
                return row
        return None

    def _research_worker(
        self,
        job_id: str,
        history: list[dict[str, object]],
    ) -> None:
        result = run_research_job(
            self.root,
            job_id,
            history=history,
        )
        self.core.reconcile_research_jobs()
        memory_failed = False
        if result.get("status") == "completed":
            try:
                ingest_verified_desktop_research(self.root, job_id)
            except (OSError, ValueError, RuntimeError):
                # A memory failure cannot discard the persisted research job.
                memory_failed = True
        answer = result.get("answer")
        if isinstance(answer, str) and answer.strip():
            verification = result.get("verification")
            verification = (
                verification
                if isinstance(verification, Mapping)
                else {}
            )
            prefix = (
                "Verified research"
                if str(verification.get("status") or "").startswith(
                    "verified_"
                )
                else "Research result"
            )
            chat_text = f"{prefix}:\n\n{answer.strip()}"
            if len(chat_text) > MAX_CHAT_CHARS:
                chat_text = (
                    chat_text[: MAX_CHAT_CHARS - 40]
                    + "\n\n[Full result remains in Research history.]"
                )
            with self._chat_lock:
                self.chat.append(
                    "assistant",
                    chat_text,
                )
        if memory_failed:
            with self._chat_lock:
                self.chat.append(
                    "assistant",
                    "Source memory indexing failed; the completed research remains in Research history.",
                )

    def start_research(
        self,
        message: str,
    ) -> dict[str, object]:
        history = self.chat.load()
        with self._research_lock:
            active = self._active_research_job()
            if active is not None:
                return {
                    "schema": "sira.desktop_research_start.v1",
                    "status": "busy",
                    "job": active,
                }
            if len(message.strip()) > 500:
                raise ValueError("Core research question must be at most 500 characters")
            dispatch = self.core.dispatch("research", text=message, allow_research=True)
            if dispatch["route"] != "research_job":
                return {"schema": "sira.desktop_research_start.v1", "status": "blocked",
                        "core_task": dispatch["task"], "job": None}
            job_id = dispatch["task"]["artifact_refs"][0]
            job = self.research_jobs.read(job_id)
            if job is None or job.get("status") != "queued":
                raise ValueError("Core research job unavailable")
            with self._chat_lock:
                user = self.chat.append("user", message)
            thread = threading.Thread(
                target=self._research_worker,
                args=(str(job["job_id"]), history),
                name=f"sira-desktop-research-{job['job_id']}",
                daemon=True,
            )
            thread.start()
        return {
            "schema": "sira.desktop_research_start.v1",
            "status": "started",
            "user": user,
            "job": job,
            "core_task": dispatch["task"],
            "identity": dispatch["identity"],
        }

    def _local_reply(
        self,
        message: str,
    ) -> str | None:
        lowered = message.casefold()
        overview = self.overview()
        runtime = overview["runtime"]
        release = overview["release"]
        memory = overview["memory"]
        notifications = overview["notifications"]

        if any(token in lowered for token in (
            "status", "running", "run hocche", "on ache",
            "off ache", "চালু", "বন্ধ", "স্ট্যাটাস",
        )):
            return (
                f"SIRA runtime is {runtime.get('effective_state')} "
                f"(desired: {runtime.get('desired_state')}, "
                f"generation: {runtime.get('generation')}). "
                f"Release ready: {bool(release.get('release_ready'))}."
            )

        if any(token in lowered for token in (
            "memory", "মেমোরি", "remember",
        )):
            counts = memory.get("counts")
            counts = (
                counts
                if isinstance(counts, Mapping)
                else {}
            )
            return (
                f"Memory database health: "
                f"{'healthy' if memory.get('healthy') else 'unhealthy'}. "
                f"Schema v{memory.get('schema_version')}; "
                f"{counts.get('memories', 0)} memories, "
                f"{counts.get('occurrences', 0)} occurrences, "
                f"{counts.get('transitions', 0)} transitions."
            )

        if any(token in lowered for token in (
            "last cycle", "last task", "শেষ", "ki korso",
            "কি করছ", "improve", "improvement",
        )):
            last = runtime.get("last_cycle")
            if isinstance(last, Mapping):
                return (
                    f"Last cycle status: {last.get('status')}; "
                    f"target: {last.get('target_kind')}; "
                    f"outcome: {last.get('outcome')}. "
                    f"Completed at {last.get('completed_at')}."
                )
            return (
                "No completed autonomous cycle is recorded yet."
            )

        if any(token in lowered for token in (
            "notification", "access request",
            "permission", "নোটিফিকেশন", "পারমিশন",
        )):
            return (
                f"Owner notification outbox is "
                f"{'readable' if notifications.get('readable') else 'unavailable'} "
                f"with {notifications.get('total', 0)} event(s)."
            )

        if any(token in lowered for token in (
            "start sira", "self on", "চালু কর",
            "start koro", "stop sira", "self off",
            "বন্ধ কর",
        )):
            return (
                "Runtime control from free-form chat is intentionally "
                "disabled. Use the explicit Start SIRA or Stop SIRA "
                "control button so a conversational message cannot "
                "change autonomous runtime state."
            )

        return None

    def chat_send(
        self,
        message: str,
        *,
        research: bool = False,
    ) -> dict[str, object]:
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= MAX_CHAT_CHARS:
            raise ValueError(f"chat text must be 1..{MAX_CHAT_CHARS} characters")
        if research:
            return self.start_research(message)
        goal_reply = learning_goal_chat_reply(self.root, message)
        if goal_reply is not None:
            reply_text, goal, mode = goal_reply
            if goal is not None and mode == "local_learning_goal" and goal["status"] == "active":
                dispatch = self.core.dispatch("learn", goal_id=goal["goal_id"])
            else:
                dispatch = self.core.dispatch("inspect_self")
            with self._chat_lock:
                user = self.chat.append("user", message)
                assistant = self.chat.append("assistant", reply_text)
            return {
                "schema": "sira.desktop_chat_reply.v2",
                "mode": mode,
                "user": user,
                "assistant": assistant,
                "model": None,
                "goal": ({"goal_id": goal["goal_id"], "status": goal["status"]}
                         if goal is not None else None),
                "core_task": dispatch["task"], "identity": dispatch["identity"],
                "provenance": [],
            }
        if explicit_research_requested(message):
            return self.start_research(message)

        history = self.chat.load()
        with self._chat_lock:
            user = self.chat.append("user", message)

        lower = message.casefold()
        control_cue = any(cue in lower for cue in (
            "start sira", "stop sira", "self on", "self off", "চালু কর", "বন্ধ কর"))
        operational_cue = control_cue or any(cue in lower for cue in (
            "status", "running", "state", "what are you doing", "capabilit", "gap",
            "স্ট্যাটাস", "last cycle", "last task", "memory", "মেমোরি", "notification"))
        engineering_cue = not operational_cue and any(cue in lower for cue in (
            "improve sira", "improve your code", "improve your tests", "self-improvement request"))
        knowledge_cue = (not operational_cue and not engineering_cue
                         and not lower.lstrip().startswith(("how do i ", "how can i "))
                         and any(cue in lower for cue in (
            "teach me", "what did you learn", "what have you learned", "explain",
            "what is ", "what are ", "why ", "how does ", "how do ",
            "tell me about", "শেখাও", "শিখেছ", "ব্যাখ্যা কর")))
        # The Core accepts at most 500 characters. Other guarded conversational
        # requests keep their existing 4000-character desktop policy.
        bounded = message[:500]
        intent = ("inspect_self" if operational_cue else "engineering" if engineering_cue
                  else "answer_from_memory" if knowledge_cue or len(message) <= 500
                  else "general")
        dispatch = self.core.dispatch(intent, text=bounded if intent != "inspect_self" else None)
        provenance = []
        verified_reply = None
        if dispatch["route"] == "verified_memory":
            reply_text, provenance = verified_memory_text(dispatch["result"])
            verified_reply = (reply_text, "local_verified_knowledge")
        elif knowledge_cue and dispatch["route"] == "research_needed":
            # Legacy phrasing is sometimes broader than A79's strict overlap.
            # Preserve its evidence-gated teaching only for a retrieval miss;
            # never override stale, contradictory or invalid lifecycle decisions.
            reason = (dispatch.get("memory_decision") or {}).get("reason")
            if reason in {"no_relevant_verified_memory", "no_verified_memory"}:
                verified_reply = verified_knowledge_reply(self.root, message)
        local_reply = self._local_reply(message) if operational_cue and control_cue else None

        model_result: dict[str, object] | None = None
        if verified_reply is not None:
            reply_text, mode = verified_reply
        elif operational_cue and local_reply is None:
            reply_text = operational_text(dispatch["state"], message)
            mode = "local_operational"
        elif local_reply is not None:
            reply_text = local_reply
            mode = "local_operational"
        elif engineering_cue:
            reply_text = ("SIRA recorded this as a direct engineering request for the existing "
                          "protected evaluation and promotion pipeline. It is pending; no code "
                          "was promoted or authorized by this chat message.")
            mode = "local_engineering_pending"
        elif knowledge_cue and lower.startswith("teach me") and _matching_pending_evidence(self.root, message):
            # Pre-A84 guarded chat remains available for a weak local draft,
            # but the Core route stays research_needed and no claim is cited.
            model_result = run_desktop_model_chat(self.root, message, history=history)
            reply_text = str(model_result.get("reply") or "SIRA could not produce a validated model reply.")
            mode = "model" if model_result.get("status") == "completed" else "model_blocked_or_failed"
        elif knowledge_cue:
            reason = (dispatch.get("memory_decision") or {}).get("reason", "no_verified_memory")
            reply_text = ("I have no matching verified local claim for that question yet "
                          f"({reason}). Explicit research and independent verification are needed.")
            mode = "local_knowledge_unavailable"
        else:
            model_result = run_desktop_model_chat(
                self.root,
                message,
                history=history,
            )
            raw_reply = model_result.get("reply")
            reply_text = (
                str(raw_reply)
                if isinstance(raw_reply, str)
                and raw_reply.strip()
                else (
                    "SIRA could not produce a validated model reply. "
                    "No protected action was performed."
                )
            )
            mode = (
                "model"
                if model_result.get("status") == "completed"
                else "model_blocked_or_failed"
            )

        with self._chat_lock:
            assistant = self.chat.append(
                "assistant",
                reply_text,
            )
        return {
            "schema": "sira.desktop_chat_reply.v2",
            "mode": mode,
            "user": user,
            "assistant": assistant,
            "identity": dispatch["identity"],
            "core_task": dispatch["task"],
            "provenance": provenance,
            "model": (
                {
                    "status": model_result.get("status"),
                    "intent": model_result.get("intent"),
                    "needs_research": model_result.get(
                        "needs_research"
                    ),
                    "access_request_id": model_result.get(
                        "access_request_id"
                    ),
                    "metrics": model_result.get("metrics"),
                    "artifact": model_result.get("artifact"),
                }
                if model_result is not None
                else None
            ),
        }


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address,
        handler,
        *,
        root: Path,
        static_dir: Path,
        session_token: str,
    ):
        super().__init__(address, handler)
        self.root = root
        self.static_dir = static_dir
        self.session_token = session_token
        self.control = DesktopControl(root)


class DesktopRequestHandler(BaseHTTPRequestHandler):
    server_version = "SIRA-Desktop/0.4"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Referrer-Policy",
            "no-referrer",
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "style-src 'self'; "
            "script-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "form-action 'self'",
        )
        self.send_header("Cache-Control", "no-store")

    def _json(
        self,
        payload: Mapping[str, object],
        status: int = 200,
    ) -> None:
        data = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _file(self, name: str, content_type: str) -> None:
        path = self.server.static_dir / name
        try:
            if path.is_symlink() or not path.is_file():
                raise FileNotFoundError
            data = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _require_session(self) -> bool:
        supplied = self.headers.get("X-SIRA-Session")
        if (
            not isinstance(supplied, str)
            or not secrets.compare_digest(
                supplied,
                self.server.session_token,
            )
        ):
            self._json(
                {
                    "status": "forbidden",
                    "error": "invalid_desktop_session",
                },
                HTTPStatus.FORBIDDEN,
            )
            return False
        return True

    def _body(self) -> dict[str, object] | None:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError:
            self._json(
                {"status": "error", "error": "invalid_content_length"},
                HTTPStatus.BAD_REQUEST,
            )
            return None
        if not 0 <= length <= MAX_REQUEST_BYTES:
            self._json(
                {"status": "error", "error": "request_too_large"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return None
        try:
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, json.JSONDecodeError):
            self._json(
                {"status": "error", "error": "invalid_json"},
                HTTPStatus.BAD_REQUEST,
            )
            return None
        if not isinstance(value, dict):
            self._json(
                {"status": "error", "error": "json_object_required"},
                HTTPStatus.BAD_REQUEST,
            )
            return None
        return value

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in {"/", "/index.html"}:
                self._file("index.html", "text/html; charset=utf-8")
            elif path == "/app.js":
                self._file("app.js", "text/javascript; charset=utf-8")
            elif path == "/styles.css":
                self._file("styles.css", "text/css; charset=utf-8")
            elif path == "/sira.svg":
                asset = self.server.root / "desktop" / "assets" / "sira.svg"
                try:
                    if asset.is_symlink() or not asset.is_file():
                        raise FileNotFoundError
                    data = asset.read_bytes()
                except OSError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(data)))
                self._security_headers()
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/session":
                self._json({
                    "schema": "sira.desktop_session.v1",
                    "token": self.server.session_token,
                })
            elif path == "/api/overview":
                self._json(self.server.control.overview())
            elif path == "/api/activity":
                self._json(self.server.control.activity())
            elif path == "/api/notifications":
                self._json(self.server.control.notifications())
            elif path == "/api/chat":
                self._json(self.server.control.chat_history())
            elif path == "/api/chat/status":
                self._json(self.server.control.chat_model_status())
            elif path == "/api/research/status":
                self._json(self.server.control.research_status())
            else:
                self._json(
                    {"status": "not_found"},
                    HTTPStatus.NOT_FOUND,
                )
        except Exception as exc:
            self._json(
                {
                    "status": "error",
                    "error": type(exc).__name__,
                },
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        if not self._require_session():
            return
        path = urlparse(self.path).path
        body = self._body()
        if body is None:
            return
        try:
            if path == "/api/runtime/start":
                result = self.server.control.runtime_start()
            elif path == "/api/runtime/stop":
                result = self.server.control.runtime_stop()
            elif path == "/api/release/refresh":
                result = self.server.control.refresh_release()
            elif path == "/api/notifications/deliver":
                result = self.server.control.deliver_notifications()
            elif path == "/api/chat":
                message = body.get("message")
                research = body.get("research", False)
                if (
                    not isinstance(message, str)
                    or type(research) is not bool
                ):
                    self._json(
                        {
                            "status": "error",
                            "error": "message_or_research_flag_invalid",
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                result = self.server.control.chat_send(
                    message,
                    research=research,
                )
            elif path == "/api/chat/settings":
                confirmed = body.get("free_tier_confirmed")
                if type(confirmed) is not bool:
                    self._json(
                        {
                            "status": "error",
                            "error": "free_tier_confirmation_required",
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                result = self.server.control.update_chat_settings(
                    confirmed
                )
            elif path == "/api/research/settings":
                confirmed = body.get(
                    "search_zero_cost_confirmed"
                )
                if type(confirmed) is not bool:
                    self._json(
                        {
                            "status": "error",
                            "error": "search_confirmation_required",
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                result = (
                    self.server.control.update_research_settings(
                        confirmed
                    )
                )
            else:
                self._json(
                    {"status": "not_found"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._json(result)
        except ValueError as exc:
            self._json(
                {
                    "status": "error",
                    "error": type(exc).__name__,
                    "message": str(exc)[:240],
                },
                HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:
            self._json(
                {
                    "status": "error",
                    "error": type(exc).__name__,
                },
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def _open_window(url: str) -> None:
    candidates = (
        ("google-chrome", ["--app=" + url, "--class=SIRA"]),
        ("google-chrome-stable", ["--app=" + url, "--class=SIRA"]),
        ("chromium", ["--app=" + url, "--class=SIRA"]),
        ("chromium-browser", ["--app=" + url, "--class=SIRA"]),
        ("brave-browser", ["--app=" + url, "--class=SIRA"]),
    )
    for command, args in candidates:
        executable = shutil.which(command)
        if executable:
            try:
                subprocess.Popen(
                    [executable, *args],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return
            except OSError:
                continue
    webbrowser.open(url, new=1, autoraise=True)


def run_desktop_server(
    root: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_window: bool = False,
) -> None:
    root = Path(root).expanduser().resolve()
    if host != DEFAULT_HOST:
        raise ValueError("SIRA Desktop may bind only to 127.0.0.1")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ValueError("port must be an integer from 1024 to 65535")

    static_dir = root / "desktop" / "static"
    required = ("index.html", "styles.css", "app.js")
    if any(
        not (static_dir / name).is_file()
        or (static_dir / name).is_symlink()
        for name in required
    ):
        raise RuntimeError("SIRA Desktop static assets are incomplete")

    token = secrets.token_urlsafe(32)
    try:
        server = _Server(
            (host, port),
            DesktopRequestHandler,
            root=root,
            static_dir=static_dir,
            session_token=token,
        )
    except OSError as exc:
        if exc.errno in {98, 48, 10048}:
            if open_window:
                _open_window(f"http://{host}:{port}/")
            return
        raise

    url = f"http://{host}:{port}/"
    print(json.dumps({
        "schema": "sira.desktop_server.v1",
        "desktop_version": "0.4",
        "status": "running",
        "url": url,
        "host": host,
        "port": port,
        "loopback_only": True,
        "session_protected_actions": True,
    }))

    if open_window:
        threading.Timer(0.35, _open_window, args=(url,)).start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SIRA localhost desktop control center."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)
    run_desktop_server(
        Path(args.root),
        host=args.host,
        port=args.port,
        open_window=args.open,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
