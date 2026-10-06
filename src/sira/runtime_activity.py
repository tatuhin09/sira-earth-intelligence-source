"""Bounded read-only view of the worker's active cycle for owner status."""
from __future__ import annotations

import json
from pathlib import Path
import re


_CYCLE_ID = re.compile(r"sc_[0-9a-f]{32}\Z")
_SELECTION_ID = re.compile(r"ats_[0-9a-f]{32}\Z")
_PHASE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_TARGET_KINDS = {"learning_goal", "opportunity", "memory", "knowledge_revalidation"}


def _read_object(path: Path, *, max_bytes: int = 2_000_000) -> dict | None:
    try:
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            return None
        if not 0 < path.stat().st_size <= max_bytes:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def _short_text(value: object, max_length: int) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= max_length and value.isprintable():
        return value
    return None


def current_cycle_summary(root: Path, generation: int) -> dict | None:
    """Report only a live generation's whitelisted journal/target fields."""
    root = Path(root).resolve()
    journal = _read_object(root / "runtime/active_cycle.json")
    if (journal is None or journal.get("schema_version") != 1
            or journal.get("kind") != "active_autonomous_cycle"
            or type(journal.get("generation")) is not int
            or journal["generation"] != generation):
        return None
    cycle_id = journal.get("cycle_id")
    phase = journal.get("phase")
    if (not isinstance(cycle_id, str) or not _CYCLE_ID.fullmatch(cycle_id)
            or not isinstance(phase, str) or not _PHASE.fullmatch(phase)):
        return None
    selection_id = journal.get("target_selection_id")
    if not isinstance(selection_id, str) or not _SELECTION_ID.fullmatch(selection_id):
        selection_id = None
    summary = {
        "cycle_id": cycle_id,
        "phase": phase,
        "started_at": _short_text(journal.get("started_at"), 48),
        "updated_at": _short_text(journal.get("updated_at"), 48),
        "target_selection_id": selection_id,
        "target": None,
    }
    if selection_id is None:
        return summary

    directory = root / "improvements/targeting/selections"
    if directory.is_symlink() or directory.parent.is_symlink():
        return summary
    selection = _read_object(directory / (selection_id + ".json"))
    if (selection is None or selection.get("kind") != "autonomous_target_selection"
            or selection.get("selection_id") != selection_id):
        return summary
    target = selection.get("target")
    if not isinstance(target, dict) or target.get("target_kind") not in _TARGET_KINDS:
        return summary

    kind = target["target_kind"]
    if kind == "learning_goal":
        goal_id = target.get("learning_goal_id")
        if not isinstance(goal_id, str) or not re.fullmatch(r"lg_[0-9a-f]{32}", goal_id):
            return summary
        summary["target"] = {
            "kind": kind,
            "learning_goal_id": goal_id,
            "topic": _short_text(target.get("topic"), 200),
        }
    elif kind == "opportunity":
        summary["target"] = {
            "kind": kind,
            "path": _short_text(target.get("path"), 300),
            "symbol": _short_text(target.get("symbol"), 160),
        }
    elif kind == "memory":
        summary["target"] = {"kind": kind, "memory_id": _short_text(target.get("memory_id"), 64)}
    else:
        summary["target"] = {"kind": kind, "knowledge_key": _short_text(target.get("knowledge_key"), 160)}
    return summary
