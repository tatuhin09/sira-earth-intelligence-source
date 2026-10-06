"""Multilingual semantic teacher/verifier bridge with zero-cost-first gating."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol

from .access_requests import (
    AccessNeed,
    AccessRequestStore,
    KIND_CREDENTIAL,
    RISK_LOW,
)
from .capability_broker import (
    STATUS_CREDENTIAL_REQUIRED,
    STATUS_READY,
    broker_decision,
)
from .config import load_key, load_optional_key
from .language_intelligence import analyze_language_with_store, unicode_tokens
from .learning_consolidation import LearningConsolidationStore
from .models import ProviderError, utc_now
from .providers.gemini_language import GeminiLanguageModel, LanguageModelBatch
from .storage import Cache, RunStore, write_json

CAPABILITY = "language_semantic_bridge"
FREE_TIER_CONFIRM_ENV = "SIRA_GEMINI_FREE_TIER_CONFIRMED"
MONTHLY_CAP_ENV = "SIRA_GEMINI_LANGUAGE_MONTHLY_CAP"
DEFAULT_MONTHLY_CAP = 100
MAX_MONTHLY_CAP = 1000
CACHE_TTL_SECONDS = 7 * 86400
MAX_TEXT_CHARS = 8000
MAX_QUERY_CHARS = 600
MAX_CANONICAL_CHARS = 1600
MAX_REASON_CHARS = 1000
MAX_MAPPING_CHARS = 160


class LanguageModel(Protocol):
    def interpret(self, text: str, profile: dict[str, object]) -> LanguageModelBatch: ...
    def verify(self, text: str, proposal: dict[str, object]) -> LanguageModelBatch: ...


def _truthy(value: object) -> bool:
    return isinstance(value, str) and value.strip().casefold() in {
        "1", "true", "yes", "on"
    }


def _monthly_cap(env: Mapping[str, str]) -> int:
    raw = str(env.get(MONTHLY_CAP_ENV, "")).strip()
    if not raw:
        return DEFAULT_MONTHLY_CAP
    if not raw.isdigit():
        raise ValueError("SIRA_GEMINI_LANGUAGE_MONTHLY_CAP must be an integer")
    value = int(raw)
    if not 2 <= value <= MAX_MONTHLY_CAP:
        raise ValueError("Gemini language monthly cap must be 2..1000 requests")
    return value


def _month() -> str:
    return utc_now()[:7]


def _ledger_path(root: Path, month: str) -> Path:
    return (
        root / "runtime" / "language_learning" /
        "gemini_usage" / f"{month}.json"
    )


def _read_ledger(root: Path, month: str) -> dict[str, object]:
    path = _ledger_path(root, month)
    empty = {
        "schema": "sira.gemini_language_usage.v1",
        "month": month,
        "executed_requests": 0,
    }
    if path.is_symlink() or not path.is_file():
        return empty
    try:
        if path.stat().st_size > 32 * 1024:
            return empty
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return empty
    count = value.get("executed_requests") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema") != "sira.gemini_language_usage.v1"
        or value.get("month") != month
        or type(count) is not int
        or count < 0
    ):
        return empty
    return value


def _write_ledger(root: Path, month: str, count: int) -> None:
    write_json(_ledger_path(root, month), {
        "schema": "sira.gemini_language_usage.v1",
        "month": month,
        "executed_requests": count,
        "note": (
            "Counts only SIRA language-model requests in this repository. "
            "External/shared-project quota is not observable."
        ),
    })


def language_cost_guard(
    root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    root = Path(root).resolve()
    env = os.environ if environ is None else environ
    confirmed = _truthy(env.get(FREE_TIER_CONFIRM_ENV))
    cap = _monthly_cap(env)
    month = _month()
    used = int(_read_ledger(root, month)["executed_requests"])
    required_requests = 2
    if not confirmed:
        allowed = False
        reason = "free_tier_confirmation_missing"
    elif used + required_requests > cap:
        allowed = False
        reason = "local_language_model_monthly_cap_reached"
    else:
        allowed = True
        reason = "owner_confirmed_zero_cost_model_use"
    return {
        "schema": "sira.language_model_cost_guard.v1",
        "allowed": allowed,
        "reason": reason,
        "free_tier_confirmed": confirmed,
        "monthly_cap": cap,
        "local_requests_this_month": used,
        "required_requests_for_uncached_bridge": required_requests,
        "external_quota_observable": False,
        "paid_spending_authorized": False,
        "billing_changes_authorized": False,
    }


def _broker_preflight(
    root: Path,
    *,
    environ: Mapping[str, str] | None,
) -> dict[str, object]:
    env = dict(os.environ if environ is None else environ)
    if load_optional_key(root, "GEMINI_API_KEY") is not None:
        env["GEMINI_API_KEY"] = "configured"
    return broker_decision(
        root,
        CAPABILITY,
        allow_metered=True,
        environ=env,
        persist=True,
    )


def _create_credential_request(
    root: Path,
    preflight: Mapping[str, object],
    *,
    task_id: str,
) -> dict[str, object] | None:
    raw = preflight.get("access_need")
    if not isinstance(raw, Mapping):
        return None
    if raw.get("kind") != KIND_CREDENTIAL:
        return None
    try:
        need = AccessNeed(
            kind=str(raw["kind"]),
            resource=str(raw["resource"]),
            reason=str(raw["reason"]),
            risk=str(raw.get("risk") or RISK_LOW),
            provider_id=(
                str(raw["provider_id"])
                if isinstance(raw.get("provider_id"), str)
                else None
            ),
            credential_name=(
                str(raw["credential_name"])
                if isinstance(raw.get("credential_name"), str)
                else None
            ),
            owner_action=(
                str(raw["owner_action"])
                if isinstance(raw.get("owner_action"), str)
                else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return AccessRequestStore(root).create(
        need,
        task_kind="language-bridge",
        task_id=task_id,
        resume_hint=(
            "Retry the multilingual semantic bridge after the required "
            "Gemini credential is verified locally."
        ),
    )


def _clean_text(value: object, *, limit: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = " ".join(value.split())
    if not value or len(value) > limit:
        raise ValueError(f"{field} is empty or too long")
    value.encode("utf-8")
    return value


def _queries(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 3:
        raise ValueError("research queries must contain at most three items")
    rows: list[str] = []
    for item in value:
        clean = _clean_text(item, limit=MAX_QUERY_CHARS, field="research query")
        if clean not in rows:
            rows.append(clean)
    if not rows:
        raise ValueError("at least one research query is required")
    return rows


def _proposal(data: Mapping[str, object], original_tokens: set[str]) -> dict[str, object]:
    canonical = _clean_text(
        data.get("canonical_english"),
        limit=MAX_CANONICAL_CHARS,
        field="canonical English",
    )
    queries = _queries(data.get("research_queries"))
    language = _clean_text(
        data.get("detected_language"),
        limit=120,
        field="detected language",
    )
    uncertainty = _clean_text(
        data.get("uncertainty"),
        limit=MAX_REASON_CHARS,
        field="uncertainty",
    )

    raw_mappings = data.get("candidate_mappings")
    if not isinstance(raw_mappings, list) or len(raw_mappings) > 8:
        raise ValueError("candidate mappings must be a bounded list")
    mappings: list[dict[str, object]] = []
    for row in raw_mappings:
        if not isinstance(row, Mapping):
            raise ValueError("mapping must be an object")
        token = _clean_text(row.get("token"), limit=64, field="mapping token").casefold()
        if any(char.isspace() for char in token) or token not in original_tokens:
            continue
        meaning = _clean_text(
            row.get("meaning"),
            limit=MAX_MAPPING_CHARS,
            field="mapping meaning",
        ).casefold()
        confidence = row.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
        ):
            raise ValueError("mapping confidence must be 0..1")
        mappings.append({
            "token": token,
            "meaning": meaning,
            "confidence": round(float(confidence), 4),
        })

    return {
        "detected_language": language,
        "canonical_english": canonical,
        "research_queries": queries,
        "candidate_mappings": mappings,
        "uncertainty": uncertainty,
    }


def _verification(
    data: Mapping[str, object],
    proposal: Mapping[str, object],
    original_tokens: set[str],
) -> dict[str, object]:
    accepted = data.get("accepted")
    equivalent = data.get("semantic_equivalent")
    intent = data.get("intent_preserved")
    if type(accepted) is not bool or type(equivalent) is not bool or type(intent) is not bool:
        raise ValueError("verification booleans required")

    canonical = _clean_text(
        data.get("verified_canonical_english"),
        limit=MAX_CANONICAL_CHARS,
        field="verified canonical English",
    )
    queries = _queries(data.get("verified_research_queries"))
    reason = _clean_text(data.get("reason"), limit=MAX_REASON_CHARS, field="reason")

    candidates = {
        (str(row["token"]), str(row["meaning"]))
        for row in proposal.get("candidate_mappings", [])
        if isinstance(row, Mapping)
    }
    raw_verdicts = data.get("mapping_verdicts")
    if not isinstance(raw_verdicts, list) or len(raw_verdicts) > 8:
        raise ValueError("mapping verdicts must be a bounded list")

    verdicts: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for row in raw_verdicts:
        if not isinstance(row, Mapping):
            raise ValueError("mapping verdict must be an object")
        token = _clean_text(row.get("token"), limit=64, field="verdict token").casefold()
        meaning = _clean_text(
            row.get("meaning"),
            limit=MAX_MAPPING_CHARS,
            field="verdict meaning",
        ).casefold()
        key = (token, meaning)
        if key not in candidates or key in seen or token not in original_tokens:
            continue
        supported = row.get("supported")
        confidence = row.get("confidence")
        if type(supported) is not bool:
            raise ValueError("mapping supported flag required")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
        ):
            raise ValueError("verdict confidence must be 0..1")
        verdicts.append({
            "token": token,
            "meaning": meaning,
            "supported": supported,
            "confidence": round(float(confidence), 4),
        })
        seen.add(key)

    final_accepted = bool(accepted and equivalent and intent)
    return {
        "accepted": final_accepted,
        "semantic_equivalent": equivalent,
        "intent_preserved": intent,
        "verified_canonical_english": canonical,
        "verified_research_queries": queries,
        "mapping_verdicts": verdicts,
        "reason": reason,
    }


def bridge_multilingual_query(
    root: Path,
    text: str,
    *,
    allow_model: bool = False,
    environ: Mapping[str, str] | None = None,
    use_cache: bool = True,
    model: LanguageModel | None = None,
) -> dict[str, object]:
    root = Path(root).resolve()
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise ValueError("text must contain 1..8000 characters")

    profile = analyze_language_with_store(root, text)
    run = RunStore(root)
    run.event("run_started", operation="language_semantic_bridge")
    task_id = "lb-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    base: dict[str, object] = {
        "schema_version": 1,
        "kind": "language_semantic_bridge",
        "run_id": run.run_id,
        "created_at": utc_now(),
        "status": "local_only",
        "language_profile": profile.to_dict(),
        "teacher_required": profile.needs_semantic_teacher,
        "teacher_used": False,
        "verifier_used": False,
        "canonical_english": profile.normalized_text,
        "research_queries": list(profile.research_seed_queries),
        "verification": None,
        "mapping_evidence_recorded": 0,
        "mapping_consolidation": [],
        "cost_guard": None,
        "capability_broker": None,
        "access_request_id": None,
        "owner_action_required": False,
        "owner_action": None,
        "metrics": {
            "api_requests": 0,
            "cache_hit": False,
            "input_tokens": 0,
            "output_tokens": 0,
        },
        "paid_spending": False,
        "billing_changes_performed": False,
        "authority_granted": False,
        "promotion_authorized": False,
    }

    if not profile.needs_semantic_teacher:
        write_json(run.path / "language-bridge.json", base)
        run.event("run_finished", status="local_only", api_requests=0)
        return base

    if not allow_model:
        base["status"] = "blocked"
        base["owner_action_required"] = False
        base["owner_action"] = (
            "Model interpretation is disabled for this call. Continue with the "
            "original/locally expanded query or explicitly enable the bounded model bridge."
        )
        write_json(run.path / "language-bridge.json", base)
        run.event("run_finished", status="blocked", reason="model_not_enabled")
        return base

    env = os.environ if environ is None else environ
    guard = language_cost_guard(root, environ=env)
    base["cost_guard"] = guard
    if not guard["allowed"]:
        base["status"] = "blocked"
        base["owner_action_required"] = guard["reason"] == "free_tier_confirmation_missing"
        base["owner_action"] = (
            "Confirm SIRA_GEMINI_FREE_TIER_CONFIRMED=true only if this Gemini "
            "project is intentionally zero-cost/no-charge. Do not enable billing."
            if base["owner_action_required"]
            else "Local language-model request cap reached; continue with safe local/free alternatives."
        )
        write_json(run.path / "language-bridge.json", base)
        run.event("run_finished", status="blocked", reason=str(guard["reason"]))
        return base

    preflight = _broker_preflight(root, environ=env)
    base["capability_broker"] = preflight
    if preflight.get("status") != STATUS_READY:
        base["status"] = "blocked"
        if preflight.get("status") == STATUS_CREDENTIAL_REQUIRED:
            access = _create_credential_request(root, preflight, task_id=task_id)
            if isinstance(access, Mapping):
                base["access_request_id"] = access.get("request_id")
            base["owner_action_required"] = True
            base["owner_action"] = (
                "GEMINI_API_KEY is required for the semantic teacher/verifier. "
                "Configure it locally; the blocked language task is parked and "
                "other safe work may continue."
            )
        else:
            base["owner_action"] = (
                "Gemini language capability is temporarily/policy unavailable; "
                "continue with the original or locally expanded query."
            )
        write_json(run.path / "language-bridge.json", base)
        run.event(
            "run_finished",
            status="blocked",
            reason=str(preflight.get("status") or "provider_unavailable"),
        )
        return base

    cache = Cache(root / ".cache" / "language_semantic_bridge", CACHE_TTL_SECONDS)
    cache_key = json.dumps(
        [profile.normalized_text, profile.language_label, "teacher-verifier-v1"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    cached = cache.get(cache_key) if use_cache else None
    if isinstance(cached, dict):
        base.update(cached)
        base["run_id"] = run.run_id
        base["created_at"] = utc_now()
        base["metrics"] = dict(base.get("metrics") or {})
        base["metrics"]["api_requests"] = 0
        base["metrics"]["cache_hit"] = True
        base["mapping_evidence_recorded"] = 0
        base["mapping_consolidation"] = []
        write_json(run.path / "language-bridge.json", base)
        run.event("run_finished", status=str(base["status"]), cache_hit=True)
        return base

    client = model or GeminiLanguageModel(load_key(root, "GEMINI_API_KEY"))
    original_tokens = set(unicode_tokens(profile.normalized_text))
    api_requests = 0
    input_tokens = 0
    output_tokens = 0

    try:
        teacher = client.interpret(text, profile.to_dict())
        api_requests += teacher.api_requests
        input_tokens += teacher.input_tokens or 0
        output_tokens += teacher.output_tokens or 0
        proposal = _proposal(teacher.data, original_tokens)

        verifier = client.verify(text, proposal)
        api_requests += verifier.api_requests
        input_tokens += verifier.input_tokens or 0
        output_tokens += verifier.output_tokens or 0
        verification = _verification(verifier.data, proposal, original_tokens)
    except ProviderError as exc:
        api_requests += max(0, int(exc.request_count or 0))
        month = _month()
        current = int(_read_ledger(root, month)["executed_requests"])
        if api_requests:
            _write_ledger(root, month, current + api_requests)
        base["status"] = "failed"
        base["metrics"] = {
            "api_requests": api_requests,
            "cache_hit": False,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        base["owner_action"] = (
            "Provider failure did not create a permission request. Continue with "
            "local/free alternatives and retry only after normal cooldown."
        )
        write_json(run.path / "language-bridge.json", base)
        run.event("run_finished", status="failed", error_code=exc.code)
        return base
    except (TypeError, ValueError, UnicodeError, RecursionError):
        month = _month()
        current = int(_read_ledger(root, month)["executed_requests"])
        if api_requests:
            _write_ledger(root, month, current + api_requests)
        base["status"] = "failed"
        base["metrics"] = {
            "api_requests": api_requests,
            "cache_hit": False,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        base["owner_action"] = (
            "Model output failed local semantic validation; no learning was recorded."
        )
        write_json(run.path / "language-bridge.json", base)
        run.event("run_finished", status="failed", reason="invalid_semantic_output")
        return base

    month = _month()
    current = int(_read_ledger(root, month)["executed_requests"])
    if api_requests:
        _write_ledger(root, month, current + api_requests)

    base["teacher_used"] = True
    base["verifier_used"] = True
    base["verification"] = verification
    base["metrics"] = {
        "api_requests": api_requests,
        "cache_hit": False,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }

    if verification["accepted"]:
        base["status"] = "completed"
        base["canonical_english"] = verification["verified_canonical_english"]
        base["research_queries"] = verification["verified_research_queries"]

        store = LearningConsolidationStore(root)
        recorded = 0
        decisions: list[dict[str, object]] = []
        for index, row in enumerate(verification["mapping_verdicts"]):
            if not isinstance(row, Mapping):
                continue
            if row.get("supported") is not True or float(row.get("confidence") or 0) < 0.80:
                continue
            evidence_id = f"{run.run_id}:map:{index}"
            inserted = store.record_language_mapping(
                str(row["token"]),
                str(row["meaning"]),
                evidence_id=evidence_id,
                confidence=float(row["confidence"]),
                verifier="gemini-independent-semantic-verifier",
                verified=True,
            )
            if inserted:
                recorded += 1
                decision = store.consolidate_language_mapping(str(row["token"]))
                decisions.append(decision.to_dict())
        base["mapping_evidence_recorded"] = recorded
        base["mapping_consolidation"] = decisions
    else:
        base["status"] = "verification_rejected"
        base["canonical_english"] = profile.normalized_text
        base["research_queries"] = list(profile.research_seed_queries)

    cache_payload = {
        key: value for key, value in base.items()
        if key not in {"run_id", "created_at", "mapping_evidence_recorded", "mapping_consolidation"}
    }
    cache_payload["mapping_evidence_recorded"] = 0
    cache_payload["mapping_consolidation"] = []
    if use_cache:
        cache.put(cache_key, cache_payload)

    write_json(run.path / "language-bridge.json", base)
    run.event(
        "run_finished",
        status=str(base["status"]),
        api_requests=api_requests,
        mapping_evidence_recorded=int(base["mapping_evidence_recorded"]),
    )
    return base
