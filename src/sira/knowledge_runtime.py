from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from .knowledge_consolidation import KnowledgeConsolidationStore
from .knowledge_mesh import search_knowledge_free, search_open_access_free
from .models import utc_now
from .storage import write_json

REVALIDATION_COOLDOWN_SECONDS = 24 * 60 * 60
MAX_LEDGER_ENTRIES = 1000


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class KnowledgeRevalidationLedger:
    def __init__(self, root: Path):
        self.path = Path(root).resolve() / "memory" / "knowledge_revalidation_state.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("items"), dict):
            return {}
        items = raw["items"]
        if len(items) > MAX_LEDGER_ENTRIES:
            return {}
        result = {}
        for key, row in items.items():
            if not isinstance(key, str) or not isinstance(row, dict):
                continue
            attempted = row.get("attempted_at_epoch")
            outcome = row.get("outcome")
            if isinstance(attempted, (int, float)) and not isinstance(attempted, bool) and attempted >= 0 and isinstance(outcome, str):
                result[key] = {
                    "attempted_at_epoch": float(attempted),
                    "attempted_at": row.get("attempted_at"),
                    "outcome": outcome[:120],
                }
        return result

    def state(self, knowledge_key: str, *, now_epoch: float) -> dict[str, Any]:
        row = self._load().get(knowledge_key)
        if row is None:
            return {"eligible": True, "remaining_seconds": 0, "last_attempt_at": None, "last_outcome": None}
        elapsed = max(0.0, float(now_epoch) - float(row["attempted_at_epoch"]))
        remaining = max(0, int(REVALIDATION_COOLDOWN_SECONDS - elapsed))
        return {
            "eligible": remaining == 0,
            "remaining_seconds": remaining,
            "last_attempt_at": row.get("attempted_at"),
            "last_outcome": row.get("outcome"),
        }

    def record(self, knowledge_key: str, *, outcome: str, attempted_at_epoch: float | None = None) -> None:
        when = time.time() if attempted_at_epoch is None else attempted_at_epoch
        if not isinstance(when, (int, float)) or isinstance(when, bool) or when < 0:
            raise ValueError("attempted_at_epoch must be non-negative")
        if not isinstance(outcome, str) or not outcome.strip():
            raise ValueError("outcome required")
        items = self._load()
        items[knowledge_key] = {
            "attempted_at_epoch": float(when),
            "attempted_at": _iso(float(when)),
            "outcome": outcome.strip()[:120],
        }
        if len(items) > MAX_LEDGER_ENTRIES:
            ordered = sorted(items.items(), key=lambda item: float(item[1]["attempted_at_epoch"]), reverse=True)
            items = dict(ordered[:MAX_LEDGER_ENTRIES])
        write_json(self.path, {"schema_version": 1, "updated_at": utc_now(), "items": items})


def knowledge_context_for_query(root: Path, query: str, *, limit: int = 3, now: str | None = None) -> dict[str, Any]:
    rows = KnowledgeConsolidationStore(Path(root).resolve()).search(
        query, limit=limit, include_stale=False, now=now
    )
    results = [
        {
            "knowledge_key": row["knowledge_key"],
            "claim": row["claim_text"],
            "confidence": row["confidence"],
            "evidence_count": row["evidence_count"],
            "source_count": row["source_count"],
            "host_count": row["host_count"],
            "last_verified_at": row["last_verified_at"],
            "freshness": row["freshness"],
            "retrieval_score": row.get("retrieval_score"),
        }
        for row in rows
    ]
    return {
        "schema": "sira.knowledge_context.v1",
        "query": " ".join(str(query).split()),
        "status": "reused" if results else "no_fresh_match",
        "mode": "local_verified_knowledge",
        "results": results,
        "result_count": len(results),
        "api_requests": 0,
        "metered_model_requests": 0,
        "paid_spending": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }


def resolve_knowledge_query(
    root: Path,
    query: str,
    *,
    allow_research_fallback: bool = False,
    continue_research_if_local: bool = False,
    knowledge_searcher: Callable[..., Mapping[str, Any]] = search_knowledge_free,
    open_access_searcher: Callable[..., Mapping[str, Any]] = search_open_access_free,
    now: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    local = knowledge_context_for_query(root, query, limit=3, now=now)
    if local["result_count"] and not (allow_research_fallback and continue_research_if_local):
        return {
            "schema": "sira.knowledge_resolution.v1",
            "status": "completed",
            "mode": "local_reuse",
            "query": local["query"],
            "local_context": local,
            "research": None,
            "refresh_performed": False,
            "verified_synthesis_required": False,
            "api_requests": 0,
            "metered_model_requests": 0,
            "paid_spending": False,
            "authority_granted": False,
            "promotion_authorized": False,
        }
    if not allow_research_fallback:
        return {
            "schema": "sira.knowledge_resolution.v1",
            "status": "insufficient_local_knowledge",
            "mode": "local_miss",
            "query": local["query"],
            "local_context": local,
            "research": None,
            "refresh_performed": False,
            "verified_synthesis_required": True,
            "api_requests": 0,
            "metered_model_requests": 0,
            "paid_spending": False,
            "authority_granted": False,
            "promotion_authorized": False,
        }

    knowledge = dict(knowledge_searcher(root, local["query"], max_results=3))
    open_access = dict(open_access_searcher(root, local["query"], max_results=3))
    api_requests = 0
    for report in (knowledge, open_access):
        metrics = report.get("metrics") if isinstance(report.get("metrics"), Mapping) else {}
        value = metrics.get("api_requests")
        if isinstance(value, int) and not isinstance(value, bool):
            api_requests += max(0, value)
    return {
        "schema": "sira.knowledge_resolution.v1",
        "status": "research_discovered",
        "mode": "free_research_fallback",
        "query": local["query"],
        "local_context": local,
        "research": {"knowledge": knowledge, "open_access": open_access},
        "refresh_performed": False,
        "verified_synthesis_required": True,
        "api_requests": api_requests,
        "metered_model_requests": 0,
        "paid_spending": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }


def knowledge_revalidation_targets(
    root: Path, *, limit: int = 10, now_epoch: float | None = None
) -> list[dict[str, Any]]:
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("knowledge revalidation limit must be 1..20")
    when = time.time() if now_epoch is None else now_epoch
    if not isinstance(when, (int, float)) or isinstance(when, bool) or when < 0:
        raise ValueError("now_epoch must be non-negative")
    root = Path(root).resolve()
    rows = KnowledgeConsolidationStore(root).revalidation_candidates(limit=limit * 2, now=_iso(float(when)))
    ledger = KnowledgeRevalidationLedger(root)
    result = []
    for row in rows:
        key = str(row["knowledge_key"])
        cooldown = ledger.state(key, now_epoch=float(when))
        if not cooldown["eligible"]:
            continue
        conflicted = row.get("freshness") == "conflicted"
        age = row.get("age_days")
        age_score = min(3650, int(float(age))) if isinstance(age, (int, float)) and not isinstance(age, bool) else 0
        score = 2000 if conflicted else 1000 + age_score
        result.append({
            "target_kind": "knowledge_revalidation",
            "priority_class": 2,
            "priority_class_name": "knowledge_revalidation",
            "knowledge_key": key,
            "claim_text": row["claim_text"],
            "knowledge_status": row["status"],
            "freshness": row["freshness"],
            "last_verified_at": row["last_verified_at"],
            "confidence": row["confidence"],
            "evidence_count": row["evidence_count"],
            "source_count": row["source_count"],
            "host_count": row["host_count"],
            "revalidation_reason": "verified_conflict" if conflicted else "stale_knowledge",
            "raw_priority_score": score,
            "effective_priority_score": score,
            "cooldown": cooldown,
            "authority_granted": False,
            "promotion_authorized": False,
            "paid_spending": False,
        })
    result.sort(key=lambda row: (-int(row["effective_priority_score"]), str(row["last_verified_at"]), str(row["knowledge_key"])))
    return result[:limit]


def revalidate_knowledge_target(
    root: Path,
    target: Mapping[str, Any],
    *,
    knowledge_searcher: Callable[..., Mapping[str, Any]] = search_knowledge_free,
    open_access_searcher: Callable[..., Mapping[str, Any]] = search_open_access_free,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    if not isinstance(target, Mapping) or target.get("target_kind") != "knowledge_revalidation":
        raise ValueError("knowledge revalidation target required")
    key = target.get("knowledge_key")
    claim = target.get("claim_text")
    if not isinstance(key, str) or not key or not isinstance(claim, str) or not claim.strip():
        raise ValueError("valid knowledge key and claim required")
    root = Path(root).resolve()
    when = time.time() if now_epoch is None else now_epoch
    current = KnowledgeConsolidationStore(root).lookup(key, now=_iso(float(when)))
    ledger = KnowledgeRevalidationLedger(root)

    if current is None:
        outcome = "knowledge_missing"
        ledger.record(key, outcome=outcome, attempted_at_epoch=float(when))
        return {
            "status": "completed", "outcome": outcome, "knowledge_key": key,
            "research": None, "refresh_performed": False, "verified_synthesis_required": False,
            "api_requests": 0, "metered_model_requests": 0,
            "promotion_performed": False, "main_tree_modified": False,
            "paid_spending": False, "authority_granted": False, "promotion_authorized": False,
        }
    if current.get("status") == "active" and current.get("freshness") == "fresh":
        outcome = "knowledge_already_fresh"
        ledger.record(key, outcome=outcome, attempted_at_epoch=float(when))
        return {
            "status": "completed", "outcome": outcome, "knowledge_key": key,
            "research": None, "refresh_performed": False, "verified_synthesis_required": False,
            "api_requests": 0, "metered_model_requests": 0,
            "promotion_performed": False, "main_tree_modified": False,
            "paid_spending": False, "authority_granted": False, "promotion_authorized": False,
        }

    resolution = resolve_knowledge_query(
        root,
        claim,
        allow_research_fallback=True,
        knowledge_searcher=knowledge_searcher,
        open_access_searcher=open_access_searcher,
        now=_iso(float(when)),
    )
    research = resolution.get("research")
    results_found = False
    if isinstance(research, Mapping):
        for name in ("knowledge", "open_access"):
            report = research.get(name)
            if isinstance(report, Mapping) and isinstance(report.get("results"), list) and report["results"]:
                results_found = True
    outcome = "verification_refresh_required" if results_found else "revalidation_sources_insufficient"
    ledger.record(key, outcome=outcome, attempted_at_epoch=float(when))
    report = {
        "schema": "sira.knowledge_revalidation.v1",
        "status": "completed",
        "outcome": outcome,
        "knowledge_key": key,
        "claim_text": claim,
        "prior_state": {k: current.get(k) for k in (
            "status", "freshness", "confidence", "last_verified_at",
            "evidence_count", "source_count", "host_count"
        )},
        "research": research,
        "refresh_performed": False,
        "verified_synthesis_required": True,
        "next_gate": "independently read evidence and verified synthesis are required before durable knowledge may change",
        "api_requests": int(resolution.get("api_requests") or 0),
        "metered_model_requests": 0,
        "promotion_performed": False,
        "main_tree_modified": False,
        "paid_spending": False,
        "authority_granted": False,
        "promotion_authorized": False,
        "created_at": utc_now(),
    }
    artifact = root / "memory" / "knowledge_revalidation" / f"{key.replace(':', '_')}.json"
    write_json(artifact, report)
    report["artifact"] = str(artifact)
    return report
