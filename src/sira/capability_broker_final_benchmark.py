"""Offline finalization benchmark for Capability Broker v1.6C."""
from __future__ import annotations

import inspect
import json
from pathlib import Path
import tempfile
from typing import Any

from .access_runtime import access_need_from_broker_decision
from .capability_broker import (
    STATUS_CREDENTIAL_REQUIRED,
    STATUS_METERED_POLICY_BLOCKED,
    STATUS_READY,
    broker_decision,
)
from .papers import papers_run
from .provider_catalog import ACCESS_KEY_REQUIRED, COST_METERED, get_provider
from .reading import read_run
from .storage import RunStore, write_json
from .synthesis import answer_run


def capability_broker_final_benchmark(root: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(root).resolve()
    results: list[dict[str, object]] = []

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)

        gemini = get_provider("gemini")
        results.append({
            "case_id": "gemini_catalog_contract",
            "passed": (
                gemini.access == ACCESS_KEY_REQUIRED
                and gemini.cost_class == COST_METERED
                and gemini.credential_env == ("GEMINI_API_KEY",)
                and "code_generation" in gemini.capabilities
                and "evidence_synthesis" in gemini.capabilities
                and gemini.read_only is True
                and gemini.executes_remote_code is False
            ),
        })

        default_block = broker_decision(
            sandbox,
            "code_generation",
            allow_metered=False,
            environ={"GEMINI_API_KEY": "configured"},
        )
        results.append({
            "case_id": "metered_model_disabled_by_default",
            "passed": (
                default_block["status"] == STATUS_METERED_POLICY_BLOCKED
                and default_block["access_need"] is None
            ),
        })

        missing = broker_decision(
            sandbox,
            "code_generation",
            allow_metered=True,
            environ={},
        )
        results.append({
            "case_id": "missing_gemini_key_is_access_need",
            "passed": (
                missing["status"] == STATUS_CREDENTIAL_REQUIRED
                and missing["access_request_created"] is False
                and isinstance(missing["access_need"], dict)
                and missing["access_need"]["credential_name"] == "GEMINI_API_KEY"
            ),
        })

        ready = broker_decision(
            sandbox,
            "code_generation",
            allow_metered=True,
            environ={"GEMINI_API_KEY": "configured"},
        )
        results.append({
            "case_id": "configured_gemini_is_eligible_when_budget_open",
            "passed": (
                ready["status"] == STATUS_READY
                and ready["selected_provider_id"] == "gemini"
                and ready["paid_spending"] is False
            ),
        })

        converted = access_need_from_broker_decision(missing)
        results.append({
            "case_id": "broker_need_converts_to_existing_access_contract",
            "passed": (
                converted is not None
                and converted.provider_id == "gemini"
                and converted.credential_name == "GEMINI_API_KEY"
            ),
        })

        rendered = json.dumps(ready, sort_keys=True)
        results.append({
            "case_id": "broker_audit_never_contains_credential_value",
            "passed": (
                "configured" not in rendered
                and ready["authority_granted"] is False
                and ready["promotion_authorized"] is False
            ),
        })

        results.append({
            "case_id": "paper_provider_remains_explicit_dependency",
            "passed": inspect.signature(papers_run).parameters["provider"].default is inspect.Parameter.empty,
        })
        results.append({
            "case_id": "reader_remains_explicit_dependency",
            "passed": inspect.signature(read_run).parameters["reader"].default is inspect.Parameter.empty,
        })
        results.append({
            "case_id": "synthesis_model_remains_explicit_dependency",
            "passed": inspect.signature(answer_run).parameters["model"].default is inspect.Parameter.empty,
        })
        results.append({
            "case_id": "broker_does_not_execute_or_create_access_request",
            "passed": (
                ready["provider_execution_performed"] is False
                and ready["access_request_created"] is False
                and missing["provider_execution_performed"] is False
                and missing["access_request_created"] is False
            ),
        })

    passed = sum(bool(row["passed"]) for row in results)
    report = {
        "schema_version": 1,
        "kind": "capability_broker_final_benchmark",
        "suite_id": "sira-capability-broker-v1.6c",
        "passed": passed,
        "failed": len(results) - passed,
        "api_requests": 0,
        "cases": results,
    }
    store = RunStore(root)
    path = store.path / "capability-broker-final-benchmark.json"
    write_json(path, report)
    return path, report
