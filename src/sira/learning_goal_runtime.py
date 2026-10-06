"""Bounded free-first research for one owner learning goal.

Search metadata is never verified evidence. Only validated document text
may produce a narrowly supported knowledge claim.
"""
from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from .knowledge_mesh import search_knowledge_free, search_open_access_free
from .knowledge_runtime import knowledge_context_for_query, resolve_knowledge_query
from .learning_goals import LearningGoalStore, RESEARCH_POLICY_VERSION
from .learning_goal_adaptive import preview_next_question, record_progression
from .memory_first import resolve_memory_first
from .learning_goal_provider_cooldown import LearningGoalProviderCooldown
from .learning_goal_followup import (search_query_for_focus, title_followup_hint,
                                     title_followup_questions)
from .learning_goal_general_learning import (
    _fetch_public_document, collect_and_verify_goal_sources,
)
from .learning_goal_source_review import review_discovered_sources
from .models import ProviderError, utc_now
from .trusted_hosts import load_trusted_hosts
from .source_fetch_health import drop_blocked_rows, observe_fetches
from .web_search_discovery import maybe_search_trusted_web
from .learning_goal_grounded_claims import _UNSET as _UNSET_CLAIM_MODEL, maybe_verify_grounded
from .owner_notifications import OwnerNotificationStore
from .storage import write_json
from .config import Settings
from .providers.arxiv import ArxivProvider
from .providers.doaj import DOAJProvider


class _PublicResearchRevoked(Exception):
    pass


def _with_web_discovery(root: Path, query: str, report: dict, now_epoch: float | None) -> dict:
    """Add budgeted, trusted-domain web results when the owner enabled them."""
    report = drop_blocked_rows(root, report, now_epoch if now_epoch is not None else time.time())
    web = maybe_search_trusted_web(root, query, now_epoch=now_epoch)
    if not web["rows"] and not web["api_requests"]:
        return report
    known = {row.get("url") for row in report.get("results") or []}
    results = list(report.get("results") or []) + [
        row for row in web["rows"] if row["url"] not in known]
    metrics = dict(report.get("metrics") or {})
    metrics["api_requests"] = int(metrics.get("api_requests", 0)) + web["api_requests"]
    metrics["result_count"] = len(results)
    return drop_blocked_rows(root, {**report, "results": results, "metrics": metrics,
                                    "web_search": {k: v for k, v in web.items() if k != "rows"}},
                             now_epoch if now_epoch is not None else time.time())


def _default_goal_open_access_search(root: Path, query: str, *, now_epoch: float | None = None, **kwargs):
    """Search bounded metadata from two free providers for alternate hosts."""
    timeout = Settings(root, max_results=3).timeout_seconds
    health = LearningGoalProviderCooldown(root)
    providers, skipped = health.filter(
        (DOAJProvider(timeout=timeout), ArxivProvider(timeout=timeout)),
        now_epoch=now_epoch,
    )
    if not providers:
        return _with_web_discovery(root, query, {
            "status": "deferred_provider_cooldown", "results": [],
            "providers_attempted": [], "provider_failures": [],
            "providers_skipped_cooldown": skipped,
            "metrics": {"result_count": 0, "provider_count": 0,
                        "api_requests": 0, "language_model_requests": 0, "cache_hit": False},
            "paid_spending": False,
        }, now_epoch)
    report = search_open_access_free(root, query, providers=providers, **kwargs)
    health.observe(report, now_epoch=now_epoch)
    return _with_web_discovery(root, query, {**report, "providers_skipped_cooldown": skipped},
                               now_epoch)


def run_learning_goal_target(
    root: Path, target: Mapping[str, Any], *,
    knowledge_searcher: Callable[..., Mapping[str, Any]] = search_knowledge_free,
    open_access_searcher: Callable[..., Mapping[str, Any]] = search_open_access_free,
    document_fetcher: Callable[[str], tuple[str, str, bytes]] | None = None,
    now_epoch: float | None = None,
    claim_model: Any = _UNSET_CLAIM_MODEL,
) -> dict[str, Any]:
    if not isinstance(target, Mapping) or target.get("target_kind") != "learning_goal":
        raise ValueError("Learning goal target required")
    root = Path(root).resolve()
    store = LearningGoalStore(root)
    goal_id = target.get("learning_goal_id")
    current = store.get(goal_id)
    if current["topic"] != target.get("topic"):
        raise ValueError("Learning goal selection is stale")
    if current["status"] != "active":
        return {"status": "completed", "outcome": "goal_paused", "learning_goal_id": goal_id,
                "api_requests": 0, "metered_model_requests": 0, "promotion_performed": False,
                "main_tree_modified": False, "verified_synthesis_performed": False}
    when = time.time() if now_epoch is None else now_epoch
    if goal_id not in {row["goal_id"] for row in store.candidates(now_epoch=when, limit=20)}:
        return {"status": "completed", "outcome": "goal_cooldown", "learning_goal_id": goal_id,
                "api_requests": 0, "metered_model_requests": 0, "promotion_performed": False,
                "main_tree_modified": False, "verified_synthesis_performed": False}
    adaptive = preview_next_question(root, current, now_epoch=when)
    if adaptive is None:
        return {"status": "completed", "outcome": "goal_adaptive_backoff", "learning_goal_id": goal_id,
                "api_requests": 0, "metered_model_requests": 0, "promotion_performed": False,
                "main_tree_modified": False, "verified_synthesis_performed": False}
    study_step_index = adaptive["study_step_index"]
    if study_step_index is not None:
        current = store.choose_study_step(goal_id, expected_index=current["study_next_index"],
                                          selected_index=study_step_index)
    research_query = adaptive["focus"]
    memory_decision = resolve_memory_first(root, research_query)
    if memory_decision["status"] == "memory_resolved":
        return {"status": "completed", "outcome": "local_verified_knowledge_reused",
                "learning_goal_id": goal_id, "memory_resolution": memory_decision,
                "api_requests": 0, "metered_model_requests": 0,
                "promotion_performed": False, "main_tree_modified": False,
                "verified_synthesis_performed": False}
    search_query, query_strategy = search_query_for_focus(current, research_query)
    if adaptive["strategy"] in {"contradiction_review", "independent_corroboration",
                                "alternate_source"}:
        search_query, query_strategy = adaptive["question"], adaptive["strategy"]

    # The start marker also prevents a crash mid-request from looping on the
    # same public topic immediately after the runtime restarts.
    store.record_attempt(goal_id, "research_started", at_epoch=when,
                         research_policy_version=RESEARCH_POLICY_VERSION)
    requests = 0
    local = knowledge_context_for_query(root, research_query)
    research: Mapping[str, Any] | None = None
    document_review: dict[str, Any] | None = None
    grounded_requests = 0

    if not current["public_research_allowed"]:
        outcome = ("partial_local_knowledge_reused" if local["result_count"]
                   else "insufficient_local_knowledge")
    else:
        partial: dict[str, Mapping[str, Any]] = {}

        def guarded(name: str, searcher: Callable[..., Mapping[str, Any]]):
            def search(search_root, _focus, *, max_results):
                nonlocal requests
                latest = store.get(goal_id)
                if latest["status"] != "active" or not latest["public_research_allowed"]:
                    raise _PublicResearchRevoked()
                result = searcher(search_root, search_query, max_results=max_results)
                partial[name] = result
                metrics = result.get("metrics") if isinstance(result, Mapping) else None
                count = metrics.get("api_requests") if isinstance(metrics, Mapping) else None
                if type(count) is int and count > 0:
                    requests += count
                return result
            return search

        try:
            resolution = resolve_knowledge_query(
                root, research_query, allow_research_fallback=True,
                continue_research_if_local=True,
                knowledge_searcher=guarded("knowledge", knowledge_searcher),
                open_access_searcher=guarded(
                    "open_access", _default_goal_open_access_search
                    if open_access_searcher is search_open_access_free
                    else open_access_searcher,
                ),
            )
            research = resolution.get("research")
            # The curated assessors import runtime state for manual recording,
            # so resolve them only after the runtime module is initialized.
            from .learning_goal_curated_runtime import has_curated_official_focus
            if (isinstance(research, Mapping) and (any(
                isinstance(research.get(kind), Mapping)
                and bool(research[kind].get("results"))
                for kind in ("knowledge", "open_access")
            ) or has_curated_official_focus(current["topic"], research_query))):
                outcome = "sources_discovered_needs_verification"
                # Explicit injected fetchers are used in isolated tests. A
                # substituted searcher alone must never trigger real network.
                if (document_fetcher is not None or
                        (knowledge_searcher is search_knowledge_free and
                         open_access_searcher is search_open_access_free)):
                    document_review = collect_and_verify_goal_sources(
                        root, goal_id, current["topic"], research,
                        fetcher=document_fetcher or _fetch_public_document,
                        focus=research_query if study_step_index is not None else None,
                    )
                    requests += document_review["api_requests"]
                    # A77 expands eligible public source classes for arbitrary
                    # topics only where the existing bounded collector has a
                    # relevant source-host gap. Known specialized checks stay
                    # in their existing path and take precedence.
                    if (document_review.get("verified_claim_count", 0) == 0
                            and not has_curated_official_focus(current["topic"], research_query)):
                        from .learning_goal_general_engine import (
                            has_general_source_gap, run_general_learning,
                        )
                        if has_general_source_gap(research_query, research,
                                                  load_trusted_hosts(root)):
                            earlier_failures = document_review.get("source_failures") or []
                            general_review = run_general_learning(
                                root, goal_id, research, question=research_query,
                                fetcher=document_fetcher or _fetch_public_document,
                            )
                            requests += general_review["api_requests"]
                            general_review["source_failures"] = (
                                list(earlier_failures) + general_review["source_failures"]
                            )[:6]
                            document_review = general_review
                    if document_review["status"] == "verified_knowledge_recorded":
                        outcome = "verified_knowledge_recorded"
                    elif document_review["status"] == "research_permission_revoked":
                        outcome = "research_permission_revoked"
                    elif document_review["status"] == "independent_source_unavailable":
                        outcome = "independent_source_unavailable"
                    elif document_review["status"] in {
                            "contradictory_evidence", "ambiguous_evidence", "stale_evidence"}:
                        outcome = document_review["status"]
                    observe_fetches(
                        root, ok_hosts=[d.get("host") for d in document_review.get("documents") or []
                                        if isinstance(d, Mapping)],
                        failures=document_review.get("source_failures") or [], now_epoch=when)
                    # Exact-sentence verification rarely fires across independent sites.
                    # An owner-enabled, budget-bounded grounded step may propose a claim
                    # that a deterministic local gate then verifies against verbatim
                    # sentences from at least two hosts. Disabled unless the owner policy
                    # enables it; it never changes outcomes otherwise.
                    if (outcome == "sources_discovered_needs_verification"
                            and document_review.get("verified_claim_count", 0) == 0
                            and not has_curated_official_focus(current["topic"], research_query)
                            and len(document_review.get("documents") or []) >= 2):
                        grounded = maybe_verify_grounded(
                            root, goal_id, research_query,
                            document_review.get("documents") or [],
                            model=claim_model, now_epoch=when)
                        grounded_summary = {k: v for k, v in grounded.items() if k != "claims"}
                        grounded_summary["claims"] = len(grounded.get("claims", []))
                        document_review = {**document_review,
                                           "grounded_claim_verification": grounded_summary}
                        grounded_requests = grounded.get("metered_model_requests", 0)
                        if grounded["status"] == "verified_knowledge_recorded":
                            outcome = "verified_knowledge_recorded"
                            document_review.update(
                                status="verified_knowledge_recorded",
                                verified_claim_count=grounded["verified_claim_count"],
                                claims=grounded["claims"])
            else:
                outcome = "research_sources_insufficient"
        except _PublicResearchRevoked:
            research = partial or None
            outcome = "research_permission_revoked"
        except (ProviderError, OSError, ValueError):
            research = partial or None
            outcome = "research_failed"

    found = outcome in {"sources_discovered_needs_verification", "verified_knowledge_recorded"}
    verified_count = (document_review or {}).get("verified_claim_count", 0)
    latest = store.get(goal_id)
    hint = (title_followup_hint(research_query, research)
            if latest["status"] == "active" and latest["public_research_allowed"]
            and outcome in {"sources_discovered_needs_verification",
                            "independent_source_unavailable", "verified_knowledge_recorded"}
            else None)
    questions = (title_followup_questions(research_query, research)
                 if hint is not None else [])
    attempted_question = (search_query[len(research_query) + 1:]
                          if query_strategy == "unverified_title_followup" else None)
    report = {
        "schema": "sira.learning_goal_research.v1",
        "status": "completed",
        "outcome": outcome,
        "research_policy_version": RESEARCH_POLICY_VERSION,
        "learning_goal_id": goal_id,
        "topic": current["topic"],
        "research_query": research_query,
        "search_query": search_query,
        "query_strategy": query_strategy,
        "adaptive_selection": adaptive,
        "memory_resolution": memory_decision,
        "followup_hint": hint,
        "followup_questions": questions,
        "study_step_index": study_step_index,
        "study_plan_size": len(current.get("study_plan", [])),
        "public_research_allowed_at_start": current["public_research_allowed"],
        "local_context": local,
        "research": research,
        "source_review": (review_discovered_sources(research_query, research)
                          if research is not None else None),
        "document_review": ({key: value for key, value in document_review.items()
                             if key != "documents"} if document_review else None),
        "verified_claim_count": verified_count,
        "goal_mastery": "partial" if verified_count or local["result_count"] else "unverified",
        "verified_synthesis_required": found,
        "verified_synthesis_performed": False,
        "api_requests": requests,
        "metered_model_requests": grounded_requests,
        "paid_spending": False,
        "authority_granted": False,
        "promotion_authorized": False,
        "promotion_performed": False,
        "main_tree_modified": False,
        "created_at": utc_now(),
    }
    artifact = root / "memory" / "learning_goal_reports" / f"{goal_id}_{uuid4().hex}.json"
    write_json(artifact, report)
    report["artifact"] = str(artifact)
    updated = store.record_attempt(goal_id, outcome, at_epoch=when, artifact=str(artifact),
                                   research_policy_version=RESEARCH_POLICY_VERSION,
                                   study_step_index=study_step_index,
                                   verified_claim_count=verified_count,
                                   research_focus=research_query, followup_hint=hint,
                                   followup_questions=questions,
                                   attempted_question_term=attempted_question)
    record_progression(root, goal_id, adaptive, outcome=outcome, report=report, at_epoch=when)
    try:
        OwnerNotificationStore(root).enqueue_learning_result(updated, report)
    except Exception:
        # A desktop notification failure must not erase a completed study attempt.
        pass
    return report
