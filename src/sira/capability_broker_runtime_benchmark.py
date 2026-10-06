from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import patch

from .opportunity_research import _default_provider_plan, _default_providers
from .storage import RunStore, write_json


def _decision(root: Path, *chain: str, status: str = "ready", access_need=None):
    return {
        "schema": "sira.capability_broker_decision.v1",
        "policy_version": 1,
        "decision_id": "cb_" + "b" * 32,
        "capability": "paper_search",
        "status": status,
        "selected_provider_id": chain[0] if chain else None,
        "fallback_provider_ids": list(chain[1:]),
        "candidate_provider_ids": list(chain),
        "access_need": access_need,
        "owner_action_required": access_need is not None,
        "allow_metered": False,
        "api_requests": 0,
        "paid_spending": False,
        "provider_execution_performed": False,
        "access_request_created": False,
        "authority_granted": False,
        "promotion_authorized": False,
        "artifact": str(root / "runtime/capability_broker/decisions/cb_test.json"),
    }


def capability_broker_runtime_benchmark(root: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(root).resolve()
    results: list[dict[str, object]] = []

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)

        with patch(
            "sira.capability_broker.broker_decision",
            return_value=_decision(sandbox, "arxiv", "crossref", "openalex", "semantic_scholar"),
        ):
            providers, audit = _default_provider_plan(sandbox)
        results.append({"case_id": "broker_chain_applied", "passed": [p.name for p in providers] == ["arxiv", "crossref", "openalex"]})
        results.append({"case_id": "unsupported_consumer_provider_filtered", "passed": audit["consumer_unsupported_provider_ids"] == ["semantic_scholar"]})

        with patch(
            "sira.capability_broker.broker_decision",
            return_value=_decision(sandbox, "openalex", "crossref", "arxiv"),
        ):
            reordered = [p.name for p in _default_providers(sandbox)]
        results.append({"case_id": "broker_reorder_reaches_execution_chain", "passed": reordered == ["openalex", "crossref", "arxiv"]})

        with patch(
            "sira.capability_broker.broker_decision",
            return_value=_decision(sandbox, status="temporarily_unavailable"),
        ):
            blocked, blocked_audit = _default_provider_plan(sandbox)
        results.append({"case_id": "unavailable_fails_closed", "passed": blocked == () and blocked_audit["fail_closed"] is True})

        need = {
            "kind": "credential",
            "resource": "provider:tavily",
            "reason": "fixture",
            "risk": "low",
            "provider_id": "tavily",
            "credential_name": "TAVILY_API_KEY",
            "owner_action": "configure locally",
        }
        with patch(
            "sira.capability_broker.broker_decision",
            return_value=_decision(sandbox, status="credential_required", access_need=need),
        ):
            credential_blocked, credential_audit = _default_provider_plan(sandbox)
        results.append({
            "case_id": "credential_metadata_does_not_execute",
            "passed": (
                credential_blocked == ()
                and credential_audit["access_need"]["credential_name"] == "TAVILY_API_KEY"
                and credential_audit["access_request_created"] is False
            ),
        })

        results.append({"case_id": "broker_never_authorizes_promotion", "passed": audit["promotion_authorized"] is False and audit["authority_granted"] is False})
        results.append({"case_id": "consumer_plan_is_free_only", "passed": audit["allow_metered"] is False and audit["paid_spending"] is False})
        results.append({"case_id": "broker_does_not_execute_provider", "passed": audit["provider_execution_performed"] is False})

    passed = sum(bool(row["passed"]) for row in results)
    report = {
        "schema_version": 1,
        "kind": "capability_broker_runtime_benchmark",
        "suite_id": "sira-capability-broker-runtime-v1.6b",
        "passed": passed,
        "failed": len(results) - passed,
        "api_requests": 0,
        "cases": results,
    }
    store = RunStore(root)
    path = store.path / "capability-broker-runtime-benchmark.json"
    write_json(path, report)
    return path, report
