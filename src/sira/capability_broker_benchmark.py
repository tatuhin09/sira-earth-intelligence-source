"""Offline benchmark for Capability Broker v1.6A."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from .capability_broker import (
    STATUS_CAPABILITY_UNAVAILABLE,
    STATUS_CREDENTIAL_REQUIRED,
    STATUS_METERED_BUDGET_BLOCKED,
    STATUS_METERED_POLICY_BLOCKED,
    STATUS_READY,
    STATUS_TEMPORARILY_UNAVAILABLE,
    broker_decision,
)
from .storage import RunStore, write_json


def _health(*rows: dict[str, object]) -> dict[str, object]:
    return {"schema": "sira.provider_health.v1", "providers": list(rows)}


def _row(
    provider_id: str,
    *,
    health: str = "ready",
    available: bool = True,
    cost: str = "free",
    policy_reason: str = "ready",
    cooldown: bool = False,
    unreliable: bool = False,
) -> dict[str, object]:
    return {
        "provider_id": provider_id,
        "display_name": provider_id,
        "health": health,
        "available_now": available,
        "cost_class": cost,
        "historical_unreliable": unreliable,
        "cooldown_active": cooldown,
        "policy_reason": policy_reason,
    }


def capability_broker_benchmark(root: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(root).resolve()
    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)

        paper_health = _health(
            _row("arxiv"), _row("crossref"), _row("openalex"), _row("semantic_scholar")
        )
        ready = broker_decision(sandbox, "paper_search", health_snapshot=paper_health)
        results.append({
            "case_id": "free_first_ready",
            "passed": (
                ready["status"] == STATUS_READY
                and ready["selected_provider_id"] == "arxiv"
                and ready["fallback_provider_ids"][:2] == ["crossref", "openalex"]
            ),
        })

        missing = broker_decision(sandbox, "does_not_exist")
        results.append({
            "case_id": "missing_capability",
            "passed": (
                missing["status"] == STATUS_CAPABILITY_UNAVAILABLE
                and missing["owner_action_required"] is False
            ),
        })

        metered_off = broker_decision(
            sandbox,
            "web_search",
            allow_metered=False,
            health_snapshot=_health(
                _row(
                    "tavily", health="policy_blocked", available=False,
                    cost="metered", policy_reason="metered_disabled",
                )
            ),
        )
        results.append({
            "case_id": "metered_disabled_no_access_request",
            "passed": (
                metered_off["status"] == STATUS_METERED_POLICY_BLOCKED
                and metered_off["access_need"] is None
            ),
        })

        missing_key = broker_decision(
            sandbox,
            "web_search",
            allow_metered=True,
            health_snapshot=_health(
                _row(
                    "tavily", health="policy_blocked", available=False,
                    cost="metered", policy_reason="required_credential_missing",
                )
            ),
        )
        results.append({
            "case_id": "required_key_creates_need_not_request",
            "passed": (
                missing_key["status"] == STATUS_CREDENTIAL_REQUIRED
                and missing_key["owner_action_required"] is True
                and missing_key["access_request_created"] is False
                and missing_key["access_need"]["credential_name"] == "TAVILY_API_KEY"
            ),
        })

        budget = broker_decision(
            sandbox,
            "web_search",
            allow_metered=True,
            environ={"TAVILY_API_KEY": "present-but-never-serialized"},
            health_snapshot=_health(
                _row(
                    "tavily", health="metered_budget_blocked", available=False,
                    cost="metered", policy_reason="ready",
                )
            ),
        )
        results.append({
            "case_id": "metered_budget_is_not_access_need",
            "passed": (
                budget["status"] == STATUS_METERED_BUDGET_BLOCKED
                and budget["access_need"] is None
            ),
        })

        fallback = broker_decision(
            sandbox,
            "paper_search",
            health_snapshot=_health(
                _row("arxiv", health="provider_cooldown", available=False, cooldown=True),
                _row("crossref"), _row("openalex"), _row("semantic_scholar"),
            ),
        )
        results.append({
            "case_id": "cooldown_uses_safe_fallback",
            "passed": (
                fallback["status"] == STATUS_READY
                and fallback["selected_provider_id"] == "crossref"
                and "arxiv" not in [fallback["selected_provider_id"], *fallback["fallback_provider_ids"]]
            ),
        })

        temporary = broker_decision(
            sandbox,
            "paper_search",
            health_snapshot=_health(
                _row("arxiv", health="provider_cooldown", available=False, cooldown=True),
                _row("crossref", health="provider_cooldown", available=False, cooldown=True),
                _row("openalex", health="provider_cooldown", available=False, cooldown=True),
                _row("semantic_scholar", health="provider_cooldown", available=False, cooldown=True),
            ),
        )
        results.append({
            "case_id": "all_cooldown_is_temporary_not_owner_access",
            "passed": (
                temporary["status"] == STATUS_TEMPORARILY_UNAVAILABLE
                and temporary["owner_action_required"] is False
            ),
        })

        audited = broker_decision(
            sandbox, "paper_search", health_snapshot=paper_health, persist=True
        )
        rendered = json.dumps(audited, sort_keys=True).casefold()
        results.append({
            "case_id": "audit_is_local_non_authoritative_and_secret_free",
            "passed": (
                Path(audited["artifact"]).is_file()
                and audited["provider_execution_performed"] is False
                and audited["authority_granted"] is False
                and audited["promotion_authorized"] is False
                and "present-but-never-serialized" not in rendered
            ),
        })

    passed = sum(bool(row["passed"]) for row in results)
    report = {
        "schema_version": 1,
        "kind": "capability_broker_benchmark",
        "suite_id": "sira-capability-broker-v1.6a",
        "passed": passed,
        "failed": len(results) - passed,
        "api_requests": 0,
        "cases": results,
        "limitations": [
            "The broker selects and audits only; it does not execute providers.",
            "AccessNeed metadata is advisory until the existing access queue creates a request.",
        ],
    }
    store = RunStore(root)
    path = store.path / "capability-broker-benchmark.json"
    write_json(path, report)
    return path, report
