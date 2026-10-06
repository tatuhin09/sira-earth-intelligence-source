"""Gap-driven learning goals: SIRA creates its own bounded study goals.

SIRA's self-model already lists capability gaps (``capability_gaps``). When the
owner enables this module, a gap with no active goal becomes a public-research
learning goal with a short study plan, so the autonomous loop has fresh, useful
work instead of idling. Every generated goal is recorded with the reason it was
created (gap, evidence references, time) so "why am I doing this?" is auditable.

Bounds: disabled by default; a small number of active generated goals; a per
capability cooldown; a minimum interval between checks; exhausted goals are
paused so slots free up. It never touches owner goals, never calls a model, never
spends money and never grants authority.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any, Mapping
from uuid import uuid4

from .learning_goals import LearningGoalStore, MAX_STUDY_STEPS

POLICY_SCHEMA = "sira.gap_goals_policy.v1"
REGISTRY_SCHEMA = "sira.gap_goals_registry.v1"
CHECK_INTERVAL_SECONDS = 15 * 60
MAX_REGISTRY_GOALS = 200
EXHAUSTED_ATTEMPTS_PER_STEP = 2

# Curated public-knowledge topics for each self-model capability. Each plan step is
# a distinct study question that public sources can answer.
GAP_TOPICS: dict[str, tuple[str, list[str]]] = {
    "research": ("Evaluating the reliability of web sources", [
        "How to assess the credibility of a web source",
        "Cross-source corroboration of factual claims",
        "Detecting contradictory claims between sources",
        "Citation and provenance practices in research"]),
    "verified_learning": ("Verifying factual claims against independent evidence", [
        "Fact checking methodology",
        "Evidence grading and strength of evidence",
        "Confirmation bias in information gathering",
        "Source independence in claim verification"]),
    "memory_retrieval": ("Information retrieval ranking and indexing", [
        "BM25 ranking function",
        "Inverted index data structure",
        "Precision and recall in information retrieval",
        "Full text search with SQLite FTS5"]),
    "knowledge_freshness": ("Keeping a knowledge base current", [
        "Knowledge base maintenance and staleness",
        "Cache invalidation strategies",
        "Data provenance and versioning",
        "Detecting outdated information"]),
    "planning": ("Task planning and prioritization for autonomous agents", [
        "Hierarchical task network planning",
        "Priority scheduling algorithms",
        "Goal decomposition in AI planning",
        "Plan monitoring and replanning"]),
    "coding_engineering": ("Safe automated code refactoring", [
        "Refactoring techniques that preserve behavior",
        "Code review best practices",
        "Cyclomatic complexity and maintainability",
        "Static analysis of Python code"]),
    "testing": ("Strategies for effective automated testing", [
        "Test oracles and the oracle problem",
        "Mutation testing",
        "Property based testing",
        "Regression testing strategies"]),
    "verification": ("Empirical and formal software verification", [
        "Software verification and validation",
        "Invariants and assertions in programs",
        "Differential testing",
        "Reproducible builds and experiments"]),
    "tool_use": ("Tool use in language model agents", [
        "Function calling in large language models",
        "Sandboxing untrusted code execution",
        "Prompt injection attacks on LLM agents",
        "Capability based security"]),
    "provider_routing": ("Failover and load balancing for external services", [
        "Circuit breaker pattern",
        "Exponential backoff and jitter",
        "Load balancing algorithms",
        "Rate limiting algorithms"]),
    "failure_recovery": ("Fault tolerance in long running systems", [
        "Fault tolerance in distributed systems",
        "Checkpointing and recovery",
        "Idempotent operations",
        "Write ahead logging"]),
    "long_horizon_execution": ("Reliable long running workflows", [
        "Durable execution and workflow engines",
        "Job queue design and leases",
        "Graceful shutdown of services",
        "Monitoring and observability of services"]),
    "skill_practice": ("Deliberate practice and skill acquisition", [
        "Deliberate practice",
        "Spaced repetition",
        "Mastery learning",
        "Learning curves and skill measurement"]),
    "self_improvement": ("Safe self improving software systems", [
        "Reward hacking and specification gaming",
        "Goodhart's law",
        "Canary releases and safe rollouts",
        "Benchmark contamination and overfitting"]),
}
_PROVIDER_PREFIX = "provider:"


def _directory(root: Path) -> Path:
    return Path(root) / "memory" / "gap_goals"


def default_policy() -> dict:
    return {"enabled": False, "max_active": 2, "capability_cooldown_days": 14,
            "public_research_allowed": True, "valid": True, "source": "default"}


def load_policy(root: Path) -> dict:
    policy = default_policy()
    path = _directory(root) / "policy.json"
    if not path.exists() and not path.is_symlink():
        return policy
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            raise ValueError("unsafe policy")
        data = json.loads(path.read_text(encoding="utf-8"))
        keys = {"schema", "enabled", "max_active", "capability_cooldown_days",
                "public_research_allowed"}
        if (not isinstance(data, dict) or set(data) != keys
                or data["schema"] != POLICY_SCHEMA
                or type(data["enabled"]) is not bool
                or type(data["public_research_allowed"]) is not bool
                or type(data["max_active"]) is not int or not 1 <= data["max_active"] <= 5
                or type(data["capability_cooldown_days"]) is not int
                or not 1 <= data["capability_cooldown_days"] <= 90):
            raise ValueError("invalid policy")
    except (OSError, ValueError, UnicodeError, TypeError):
        return {**policy, "valid": False, "source": "malformed"}
    return {"enabled": data["enabled"], "max_active": data["max_active"],
            "capability_cooldown_days": data["capability_cooldown_days"],
            "public_research_allowed": data["public_research_allowed"],
            "valid": True, "source": "owner_policy"}


def write_policy(root: Path, *, enabled: bool, max_active: int = 2,
                 capability_cooldown_days: int = 14,
                 public_research_allowed: bool = True) -> dict:
    if (type(enabled) is not bool or type(public_research_allowed) is not bool
            or type(max_active) is not int or not 1 <= max_active <= 5
            or type(capability_cooldown_days) is not int
            or not 1 <= capability_cooldown_days <= 90):
        raise ValueError("max_active must be 1..5 and cooldown 1..90 days")
    directory = _directory(root)
    if directory.is_symlink() or Path(root, "memory").is_symlink():
        raise ValueError("unsafe policy directory")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".policy.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps({
        "schema": POLICY_SCHEMA, "enabled": enabled, "max_active": max_active,
        "capability_cooldown_days": capability_cooldown_days,
        "public_research_allowed": public_research_allowed}), encoding="utf-8")
    os.replace(temporary, directory / "policy.json")
    return load_policy(root)


def _load_registry(root: Path) -> dict | None:
    """``None`` means present but unreadable (fail closed); absent means empty."""
    path = _directory(root) / "registry.json"
    if not path.exists() and not path.is_symlink():
        return {"schema": REGISTRY_SCHEMA, "last_check_epoch": 0.0, "goals": [],
                "capability_last_created": {}}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema") != REGISTRY_SCHEMA
                or not isinstance(data.get("goals"), list)
                or len(data["goals"]) > MAX_REGISTRY_GOALS
                or not isinstance(data.get("capability_last_created"), dict)
                or not isinstance(data.get("last_check_epoch"), (int, float))
                or any(not isinstance(g, dict) or not isinstance(g.get("goal_id"), str)
                       or g.get("state") not in {"active", "retired"} for g in data["goals"])):
            return None
        return data
    except (OSError, ValueError, UnicodeError, TypeError):
        return None


def _save_registry(root: Path, registry: Mapping) -> None:
    directory = _directory(root)
    if directory.is_symlink() or Path(root, "memory").is_symlink():
        raise ValueError("unsafe registry directory")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".registry.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps(dict(registry), sort_keys=True), encoding="utf-8")
    os.replace(temporary, directory / "registry.json")


def _result(status: str, **extra: Any) -> dict:
    return {"engine": "gap_goal_generator_v1", "status": status, "created": [],
            "retired": [], "model_requests": 0, "api_requests": 0, "paid_requests": 0,
            "paid_spending": False, "authority_granted": False,
            "promotion_performed": False, "skill_activated": False, **extra}


def _exhausted(row: Mapping) -> bool:
    plan = row.get("study_plan")
    history = row.get("study_history")
    if not isinstance(plan, list) or not isinstance(history, list) or not plan:
        return False
    counts = [0] * len(plan)
    for item in history:
        index = item.get("index") if isinstance(item, Mapping) else None
        if type(index) is int and 0 <= index < len(plan):
            counts[index] += 1
    return all(count >= EXHAUSTED_ATTEMPTS_PER_STEP for count in counts)


def _topic_for(capability: str) -> tuple[str, list[str]] | None:
    if capability.startswith(_PROVIDER_PREFIX):
        return GAP_TOPICS["provider_routing"]
    return GAP_TOPICS.get(capability)


def _cooldown_key(capability: str) -> str:
    return "provider_routing" if capability.startswith(_PROVIDER_PREFIX) else capability


def ensure_gap_goals(root: Path, *, now_epoch: float | None = None,
                     model: Mapping | None = None, force: bool = False) -> dict:
    """Retire exhausted generated goals, then create at most one goal from a gap."""
    root = Path(root).resolve()
    policy = load_policy(root)
    if not policy["valid"]:
        return _result("policy_invalid")
    if not policy["enabled"]:
        return _result("disabled")
    when = time.time() if now_epoch is None else float(now_epoch)
    registry = _load_registry(root)
    if registry is None:
        return _result("registry_invalid")
    if not force and when - float(registry["last_check_epoch"]) < CHECK_INTERVAL_SECONDS:
        return _result("throttled")
    store = LearningGoalStore(root)
    result = _result("no_eligible_gap")
    rows = {row["goal_id"]: row for row in store.list()}

    # 1. Free slots: pause generated goals whose study plan is exhausted.
    for entry in registry["goals"]:
        if entry["state"] != "active":
            continue
        row = rows.get(entry["goal_id"])
        if row is None or row["status"] != "active":
            entry["state"], entry["retired_at_epoch"] = "retired", when
            entry["retired_reason"] = "goal_removed_or_paused"
            result["retired"].append(entry["goal_id"])
        elif _exhausted(row):
            store.set_status(entry["goal_id"], "paused")
            entry["state"], entry["retired_at_epoch"] = "retired", when
            entry["retired_reason"] = "study_plan_exhausted"
            result["retired"].append(entry["goal_id"])
    registry["last_check_epoch"] = when
    active = [e for e in registry["goals"] if e["state"] == "active"]
    if len(active) >= policy["max_active"]:
        result["status"] = "at_capacity"
        _save_registry(root, registry)
        return result

    # 2. Pick the first gap that has a topic, no cooldown and no existing goal.
    if model is None:
        from .self_model import SelfModelStore, collect_self_model
        model = collect_self_model(root, previous=SelfModelStore(root).load())
    gaps = model.get("gaps") if isinstance(model, Mapping) else None
    if not isinstance(gaps, list):
        _save_registry(root, registry)
        return _result("self_model_unavailable", retired=result["retired"])
    existing = {row["topic"].casefold() for row in rows.values()}
    cooldown_seconds = policy["capability_cooldown_days"] * 86_400
    for gap in gaps:
        capability = gap.get("capability") if isinstance(gap, Mapping) else None
        mapped = _topic_for(capability) if isinstance(capability, str) else None
        if mapped is None:
            continue
        topic, plan = mapped
        last = registry["capability_last_created"].get(_cooldown_key(capability))
        if isinstance(last, (int, float)) and when - last < cooldown_seconds:
            continue
        if topic.casefold() in existing or len(plan) > MAX_STUDY_STEPS:
            continue
        goal = store.create(topic, priority="normal",
                            public_research_allowed=policy["public_research_allowed"])
        store.set_plan(goal["goal_id"], list(plan))
        registry["goals"] = (registry["goals"] + [{
            "goal_id": goal["goal_id"], "state": "active", "topic": topic,
            "capability": capability, "gap_reason": gap.get("reason"),
            "gap_state": gap.get("state"),
            "evidence_refs": [r for r in (gap.get("evidence_refs") or [])
                              if isinstance(r, str)][:4],
            "created_at_epoch": when,
            "why": "capability gap with no active study goal"}])[-MAX_REGISTRY_GOALS:]
        registry["capability_last_created"][_cooldown_key(capability)] = when
        result["created"].append({"goal_id": goal["goal_id"], "topic": topic,
                                  "capability": capability,
                                  "gap_reason": gap.get("reason")})
        result["status"] = "created"
        break
    _save_registry(root, registry)
    return result


def maybe_ensure_gap_goals(root: Path, *, now_epoch: float | None = None) -> dict:
    """Wrapper for the scheduler: can never break target selection."""
    try:
        return ensure_gap_goals(root, now_epoch=now_epoch)
    except Exception as exc:  # noqa: BLE001 - background helper must fail safe
        return _result("gap_goal_error", reason=type(exc).__name__)
