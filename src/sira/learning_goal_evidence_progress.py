"""Attribute fresh claims to study steps through saved and rechecked source bytes."""
from __future__ import annotations

import hashlib
import json
from itertools import combinations
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit

from .knowledge_consolidation import KnowledgeConsolidationStore
from .storage import write_json


_MAX_RECENT_ARTIFACTS = 32
_MAX_INITIAL_ARTIFACTS = 8192
_MAX_ARTIFACT_BYTES = 1_000_000
_MAX_INDEX_BYTES = 64_000
_MAX_INDEX_ENTRIES = 128


def _load_index(path: Path, goal_id: str) -> list[dict[str, str]] | None:
    if path.is_symlink():
        return None
    try:
        if not 0 < path.stat().st_size <= _MAX_INDEX_BYTES:
            return None
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None
    if (not isinstance(saved, dict) or saved.get("schema_version") != 1
            or saved.get("goal_id") != goal_id or not isinstance(saved.get("entries"), list)
            or not saved["entries"] or len(saved["entries"]) > _MAX_INDEX_ENTRIES):
        return None
    entries = []
    for row in saved["entries"]:
        if (not isinstance(row, dict) or not isinstance(row.get("filename"), str)
                or not re.fullmatch(re.escape(goal_id) + r"_[0-9a-f]{32}\.json", row["filename"])
                or not isinstance(row.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])):
            return None
        entries.append({"filename": row["filename"], "sha256": row["sha256"]})
    return entries


def verified_study_step_evidence(root: Path, goal: Mapping[str, object]) -> dict[str, list[str]]:
    """Recheck indexed artifacts and discover recently verified ones.

    The index stores references only. It never grants verified status without
    matching artifact bytes and fresh, independently hosted knowledge rows.
    """
    root = Path(root).resolve()
    goal_id = goal.get("goal_id")
    plan = goal.get("study_plan")
    if (not isinstance(goal_id, str) or not re.fullmatch(r"lg_[0-9a-f]{32}", goal_id)
            or not isinstance(plan, list) or not all(isinstance(step, str) for step in plan)):
        return {}
    db = root / "memory" / "sira_knowledge.sqlite3"
    directory = root / "memory" / "learning_goal_documents"
    index_dir = root / "memory" / "learning_goal_progress"
    if (db.is_symlink() or not db.is_file() or directory.parent.is_symlink()
            or directory.is_symlink() or not directory.is_dir() or index_dir.is_symlink()):
        return {}
    index_path = index_dir / f"{goal_id}.json"
    index = _load_index(index_path, goal_id)
    recent = []
    for path in directory.glob(f"{goal_id}_*.json"):
        try:
            if (path.is_symlink() or not path.is_file()
                    or not re.fullmatch(re.escape(goal_id) + r"_[0-9a-f]{32}\.json", path.name)):
                continue
            stat = path.stat()
        except OSError:
            continue
        if 0 < stat.st_size <= _MAX_ARTIFACT_BYTES:
            recent.append((stat.st_mtime_ns, path))

    # Discover older manual claims once; subsequent displays check the compact
    # index and only scan recent artifacts for new claims.
    limit = _MAX_RECENT_ARTIFACTS if index is not None else _MAX_INITIAL_ARTIFACTS
    candidates = [(directory / row["filename"], row["sha256"]) for row in index or []]
    candidates += [(path, None) for _, path in sorted(recent, reverse=True)[:limit]]
    claims: dict[str, dict[str, str]] = {}
    retained: dict[str, dict[str, str]] = {}
    seen = set()
    store = KnowledgeConsolidationStore(root)
    for path, expected_sha in candidates:
        if path.name in seen or path.is_symlink():
            continue
        seen.add(path.name)
        try:
            if not path.is_file() or not 0 < path.stat().st_size <= _MAX_ARTIFACT_BYTES:
                continue
            raw = path.read_bytes()
            if not 0 < len(raw) <= _MAX_ARTIFACT_BYTES:
                continue
            artifact_sha = hashlib.sha256(raw).hexdigest()
            if expected_sha is not None and expected_sha != artifact_sha:
                continue
            saved = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            continue
        if (not isinstance(saved, dict)
                or saved.get("schema") != "sira.learning_goal_general_documents.v1"
                or saved.get("learning_goal_id") != goal_id
                or saved.get("topic") != goal.get("topic")
                or saved.get("research_query") not in plan
                or saved.get("status") != "quotes_need_claim_verification"
                or not isinstance(saved.get("documents"), list)
                or len(saved["documents"]) not in (2, 3)):
            continue
        urls = set()
        pairs = []
        hosts = set()
        for row in saved["documents"]:
            if (not isinstance(row, dict) or not isinstance(row.get("url"), str)
                    or not isinstance(row.get("text"), str)):
                break
            url, content = row["url"], row["text"]
            try:
                parsed = urlsplit(url)
                host = parsed.hostname
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            except (ValueError, UnicodeError):
                break
            if (parsed.scheme != "https" or row.get("host") != host
                    or not 40 <= len(content) <= 80_000
                    or row.get("content_sha256") != digest):
                break
            urls.add(url)
            hosts.add(host)
            pairs.append((url, digest))
        if (len(urls) != len(saved["documents"])
                or len(hosts) != len(saved["documents"])
                or len(pairs) != len(saved["documents"])):
            continue
        focus = saved["research_query"]
        supported = False
        for pair in combinations(pairs, 2):
            source_urls = {pair[0][0], pair[1][0]}
            digests = {artifact_sha}
            for ordered in (pair, tuple(sorted(pair))):
                digests.add(hashlib.sha256(json.dumps(
                    ordered, ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8")).hexdigest())
            for evidence_sha in digests:
                for row in store.verified_claims_for_artifact(evidence_sha):
                    if set(row["source_urls"]) == source_urls:
                        claims.setdefault(focus, {})[str(row["knowledge_key"])] = str(row["claim"])
                        supported = True
        # Keep previously verified references through temporary staleness;
        # only the live knowledge lookup determines displayed progress.
        if expected_sha is not None or supported:
            retained[path.name] = {"filename": path.name, "sha256": artifact_sha}

    if not index_path.is_symlink():
        entries = list(retained.values())[:_MAX_INDEX_ENTRIES]
        if (entries or index is not None) and (index is None or entries != index):
            index_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json(index_path, {"schema_version": 1, "goal_id": goal_id,
                                    "entries": entries})
    return {focus: list(rows.values()) for focus, rows in claims.items()}
