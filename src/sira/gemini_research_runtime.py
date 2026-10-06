"""Zero-cost-first runtime guard for Gemini research tools.

The transport can support Google Search and URL Context, but autonomous execution
fails closed unless the owner has explicitly confirmed the relevant zero-cost
condition locally. This module never enables billing or changes an external plan.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .capability_broker import STATUS_READY, broker_decision
from .config import load_key, load_optional_key
from .models import ProviderError, utc_now
from .providers.gemini_research import (
    MODE_COMBINED,
    MODE_GOOGLE_SEARCH,
    MODE_URL_CONTEXT,
    GeminiResearchBatch,
    GeminiResearchModel,
)
from .storage import Cache, RunStore, write_json


FREE_TIER_CONFIRM_ENV = "SIRA_GEMINI_FREE_TIER_CONFIRMED"
SEARCH_ZERO_COST_CONFIRM_ENV = "SIRA_GEMINI_SEARCH_ZERO_COST_CONFIRMED"
SEARCH_MONTHLY_CAP_ENV = "SIRA_GEMINI_SEARCH_MONTHLY_CAP"
DEFAULT_SEARCH_MONTHLY_CAP = 100
MAX_SEARCH_MONTHLY_CAP = 1000
CACHE_TTL_SECONDS = 86400


def _truthy(value: object) -> bool:
    return isinstance(value, str) and value.strip().casefold() in {"1", "true", "yes", "on"}


def _monthly_cap(env: Mapping[str, str]) -> int:
    raw = str(env.get(SEARCH_MONTHLY_CAP_ENV, "")).strip()
    if not raw:
        return DEFAULT_SEARCH_MONTHLY_CAP
    if not raw.isdigit():
        raise ValueError("SIRA_GEMINI_SEARCH_MONTHLY_CAP must be an integer")
    value = int(raw)
    if not 1 <= value <= MAX_SEARCH_MONTHLY_CAP:
        raise ValueError("Gemini Search monthly cap must be 1..1000")
    return value


def _month_key(created_at: str) -> str:
    return created_at[:7]


def _ledger_path(root: Path, month: str) -> Path:
    return root / "runtime" / "research_mesh" / "gemini_search_usage" / f"{month}.json"


def _safe_ledger(root: Path, month: str) -> dict[str, Any]:
    path = _ledger_path(root, month)
    if path.is_symlink() or not path.exists():
        return {"schema": "sira.gemini_search_usage.v1", "month": month, "executed_requests": 0}
    try:
        if not path.is_file() or path.stat().st_size > 32 * 1024:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return {"schema": "sira.gemini_search_usage.v1", "month": month, "executed_requests": 0}
    count = value.get("executed_requests")
    if (
        value.get("schema") != "sira.gemini_search_usage.v1"
        or value.get("month") != month
        or type(count) is not int
        or count < 0
    ):
        return {"schema": "sira.gemini_search_usage.v1", "month": month, "executed_requests": 0}
    return value


def _record_search_request(root: Path, month: str, count: int) -> None:
    path = _ledger_path(root, month)
    write_json(path, {
        "schema": "sira.gemini_search_usage.v1",
        "month": month,
        "executed_requests": count,
        "note": "Local SIRA requests only; external/shared-project usage is not observable here.",
    })


def research_cost_guard(
    root: Path,
    mode: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    env = os.environ if environ is None else environ
    if mode not in {MODE_URL_CONTEXT, MODE_GOOGLE_SEARCH, MODE_COMBINED}:
        raise ValueError("unsupported Gemini research tool mode")

    free_tier_confirmed = _truthy(env.get(FREE_TIER_CONFIRM_ENV))
    search_confirmed = _truthy(env.get(SEARCH_ZERO_COST_CONFIRM_ENV))
    cap = _monthly_cap(env)
    now = utc_now()
    month = _month_key(now)
    ledger = _safe_ledger(root, month)
    used = int(ledger["executed_requests"])
    needs_search = mode in {MODE_GOOGLE_SEARCH, MODE_COMBINED}

    allowed = free_tier_confirmed
    reason = "free_tier_confirmation_missing"
    if free_tier_confirmed:
        reason = "url_context_zero_cost_owner_confirmed"
    if needs_search:
        if not search_confirmed:
            allowed = False
            reason = "google_search_zero_cost_confirmation_missing"
        elif used >= cap:
            allowed = False
            reason = "local_google_search_monthly_cap_reached"
        elif free_tier_confirmed:
            allowed = True
            reason = "google_search_zero_cost_owner_confirmed_with_local_cap"

    return {
        "schema": "sira.gemini_research_cost_guard.v1",
        "mode": mode,
        "allowed": allowed,
        "reason": reason,
        "free_tier_confirmed": free_tier_confirmed,
        "search_zero_cost_confirmed": search_confirmed,
        "search_monthly_cap": cap,
        "local_search_requests_this_month": used,
        "month": month,
        "paid_spending_authorized": False,
        "billing_changes_authorized": False,
        "external_quota_observable": False,
    }


def _broker_preflight(
    root: Path,
    capability: str,
    *,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    env = os.environ if environ is None else environ
    configured = load_optional_key(root, "GEMINI_API_KEY") is not None
    presence_only = dict(env)
    if configured:
        presence_only["GEMINI_API_KEY"] = "configured"
    decision = broker_decision(
        root,
        capability,
        allow_metered=True,
        environ=presence_only,
        persist=True,
    )
    return decision


def run_gemini_research(
    root: Path,
    query: str,
    *,
    urls: tuple[str, ...] = (),
    mode: str = MODE_URL_CONTEXT,
    environ: Mapping[str, str] | None = None,
    use_cache: bool = True,
    model: GeminiResearchModel | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    env = os.environ if environ is None else environ
    capability = (
        "url_context"
        if mode == MODE_URL_CONTEXT
        else "google_search_grounding"
    )

    guard = research_cost_guard(root, mode, environ=env)
    preflight = _broker_preflight(root, capability, environ=env)
    run = RunStore(root)
    run.event("run_started", operation="gemini_research", mode=mode)

    base = {
        "schema_version": 1,
        "kind": "gemini_research_tool_run",
        "run_id": run.run_id,
        "created_at": utc_now(),
        "mode": mode,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "url_count": len(urls),
        "cost_guard": guard,
        "capability_broker": preflight,
        "status": "blocked",
        "error": None,
        "result": None,
        "metrics": {
            "api_requests": 0,
            "cache_hit": False,
            "input_tokens": 0,
            "output_tokens": 0,
        },
        "paid_spending": False,
        "billing_changes_performed": False,
        "access_request_created": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }

    if not guard["allowed"]:
        base["error"] = {"code": guard["reason"]}
        write_json(run.path / "gemini-research.json", base)
        run.event("run_finished", status="blocked", reason=guard["reason"])
        return base

    if preflight.get("status") != STATUS_READY:
        base["error"] = {"code": str(preflight.get("status") or "provider_unavailable")}
        write_json(run.path / "gemini-research.json", base)
        run.event("run_finished", status="blocked", reason=base["error"]["code"])
        return base

    cache = Cache(root / ".cache" / "gemini_research_tools", CACHE_TTL_SECONDS)
    cache_key = json.dumps(
        [mode, query, list(urls), "v1"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    cached = cache.get(cache_key) if use_cache else None
    if isinstance(cached, dict):
        base["status"] = "completed"
        base["result"] = cached
        base["metrics"]["cache_hit"] = True
        write_json(run.path / "gemini-research.json", base)
        run.event("run_finished", status="completed", cache_hit=True)
        return base

    client = model or GeminiResearchModel(load_key(root, "GEMINI_API_KEY"))
    try:
        batch = client.research(query, urls=urls, mode=mode)
    except ProviderError as exc:
        base["status"] = "failed"
        base["error"] = {
            "code": exc.code,
            "retry_after_seconds": exc.retry_after,
        }
        base["metrics"]["api_requests"] = exc.request_count
        write_json(run.path / "gemini-research.json", base)
        run.event("run_finished", status="failed", error_code=exc.code)
        return base

    result = batch.to_dict()
    if use_cache:
        cache.put(cache_key, result)

    base["status"] = "completed"
    base["result"] = result
    base["metrics"] = {
        "api_requests": batch.api_requests,
        "cache_hit": False,
        "input_tokens": batch.input_tokens or 0,
        "output_tokens": batch.output_tokens or 0,
    }

    if mode in {MODE_GOOGLE_SEARCH, MODE_COMBINED} and batch.api_requests:
        month = str(guard["month"])
        current = _safe_ledger(root, month)
        _record_search_request(
            root,
            month,
            int(current["executed_requests"]) + 1,
        )

    write_json(run.path / "gemini-research.json", base)
    run.event("run_finished", status="completed", api_requests=batch.api_requests)
    return base
