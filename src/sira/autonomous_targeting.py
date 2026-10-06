"""Unified local target selection for autonomous improvement.

v1.0D-1 only selects and ranks targets. It does not research, invoke a model,
modify source, evaluate candidates, promote changes, or control the persistent
worker. Opportunity lineage is stable across source fingerprints so a recent
successful refactor cannot immediately re-enter through a changed hash.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable
from uuid import uuid4

from .auto_evaluator import benchmark_evidence_for_target
from .runtime_evaluator_feedback import runtime_evaluator_feedback_summary
from .memory import MemoryStore
from .knowledge_runtime import knowledge_revalidation_targets
from .learning_goals import LearningGoalStore
from .models import utc_now
from .opportunity import MAX_DISCOVERY_LIMIT, discover_opportunities
from .opportunity_evidence import opportunity_readiness_preview
from .storage import write_json
from .access_runtime import target_identity

TARGET_POLICY_VERSION = 3
LINEAGE_POLICY_VERSION = 1
PROMOTION_SUPPRESSION_SECONDS = 24 * 60 * 60
DIMINISHING_WINDOW_SECONDS = 30 * 24 * 60 * 60
PENALTY_PER_RECENT_PROMOTION = 80
MAX_PRIORITY_PENALTY = 240
MAX_LINEAGES = 1000
MAX_PROMOTIONS_PER_LINEAGE = 20
MAX_HANDOFF_IMPORTS = 1000
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")


class TargetSelectionError(ValueError):
    pass


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def opportunity_lineage_key(opportunity: dict[str, Any]) -> str:
    if not isinstance(opportunity, dict):
        raise TargetSelectionError("Opportunity must be an object")
    kind = opportunity.get("type")
    path = opportunity.get("path")
    symbol = opportunity.get("symbol")
    if not isinstance(kind, str) or not kind.strip():
        raise TargetSelectionError("Opportunity type is invalid")
    if not isinstance(path, str) or not path.startswith("src/sira/"):
        raise TargetSelectionError("Opportunity path is invalid")
    if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
        raise TargetSelectionError("Opportunity symbol is invalid")
    payload = {"type": kind.strip(), "path": path, "symbol": symbol}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class OpportunityLineageStore:
    """Bounded local history keyed by stable opportunity type/path/symbol."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.base = self.root / "improvements" / "opportunities"
        self.path = self.base / "lineage_history.json"
        self.handoffs = self.base / "handoffs"

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TargetSelectionError("Opportunity lineage history is corrupt") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("lineages"), dict):
            raise TargetSelectionError("Opportunity lineage history format is invalid")
        lineages = raw["lineages"]
        if len(lineages) > MAX_LINEAGES:
            raise TargetSelectionError("Opportunity lineage history is too large")
        for key, item in lineages.items():
            if not isinstance(key, str) or not _FINGERPRINT_RE.fullmatch(key) or not isinstance(item, dict):
                raise TargetSelectionError("Opportunity lineage history contains invalid entries")
            promotions = item.get("promotions")
            if not isinstance(promotions, list) or len(promotions) > MAX_PROMOTIONS_PER_LINEAGE:
                raise TargetSelectionError("Opportunity lineage promotions are invalid")
            for event in promotions:
                if (not isinstance(event, dict)
                        or not isinstance(event.get("attempted_at_epoch"), (int, float))
                        or isinstance(event.get("attempted_at_epoch"), bool)):
                    raise TargetSelectionError("Opportunity lineage promotion event is invalid")
        return lineages

    def _save(self, lineages: dict[str, dict[str, Any]]) -> None:
        if len(lineages) > MAX_LINEAGES:
            ordered = sorted(
                lineages.items(),
                key=lambda item: max(
                    (float(event.get("attempted_at_epoch", 0)) for event in item[1].get("promotions", [])),
                    default=0.0,
                ),
                reverse=True,
            )
            lineages = dict(ordered[:MAX_LINEAGES])
        payload = {
            "schema_version": 1,
            "policy_version": LINEAGE_POLICY_VERSION,
            "updated_at": utc_now(),
            "lineages": lineages,
        }
        write_json(self.path, payload)

    @staticmethod
    def _event_identity(event: dict[str, Any]) -> tuple[Any, ...]:
        return (
            event.get("source_handoff_id"),
            event.get("opportunity_id"),
            float(event.get("attempted_at_epoch", 0)),
            event.get("outcome"),
        )

    def record_promotion(
        self,
        opportunity: dict[str, Any],
        *,
        outcome: str,
        attempted_at_epoch: float | None = None,
        source_handoff_id: str | None = None,
    ) -> bool:
        if not isinstance(outcome, str) or not 1 <= len(outcome.strip()) <= 120:
            raise TargetSelectionError("Lineage outcome must be 1..120 characters")
        when = time.time() if attempted_at_epoch is None else attempted_at_epoch
        if not isinstance(when, (int, float)) or isinstance(when, bool) or when < 0:
            raise TargetSelectionError("attempted_at_epoch must be a non-negative number")
        lineage_key = opportunity_lineage_key(opportunity)
        lineages = self._load()
        entry = lineages.setdefault(lineage_key, {
            "type": opportunity.get("type"),
            "path": opportunity.get("path"),
            "symbol": opportunity.get("symbol"),
            "promotions": [],
        })
        event = {
            "source_handoff_id": source_handoff_id,
            "opportunity_id": opportunity.get("opportunity_id"),
            "fingerprint": opportunity.get("fingerprint"),
            "outcome": outcome.strip(),
            "attempted_at_epoch": float(when),
            "attempted_at": _iso_from_epoch(float(when)),
        }
        identity = self._event_identity(event)
        if any(self._event_identity(existing) == identity for existing in entry["promotions"]):
            return False
        entry["promotions"].append(event)
        entry["promotions"].sort(key=lambda row: float(row["attempted_at_epoch"]), reverse=True)
        del entry["promotions"][MAX_PROMOTIONS_PER_LINEAGE:]
        self._save(lineages)
        return True

    @staticmethod
    def _small_artifact(path: Path) -> dict[str, Any] | None:
        try:
            if (path.is_symlink() or not path.is_file()
                    or path.stat().st_size > MAX_ARTIFACT_BYTES):
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _proven_reverted_handoff(self, data: dict[str, Any], handoff_id: str) -> bool:
        """Recognize a reverted target without changing historical promotion audit."""
        if (data.get("runtime_route") != "protected_engineering_v1"
                or data.get("handoff_id") != handoff_id):
            return False
        promotion = data.get("promotion")
        cooldown = data.get("cooldown")
        research_id = data.get("research_id")
        if (not isinstance(promotion, dict)
                or promotion.get("schema") != "sira.engineering_promotion_report.v1"
                or promotion.get("promotion_performed") is not True
                or not isinstance(cooldown, dict)
                or not isinstance(research_id, str)
                or not re.fullmatch(r"or_[0-9a-f]{32}", research_id)):
            return False
        research = self._small_artifact(self.base / "research" / f"{research_id}.json")
        if (research is None or research.get("research_id") != research_id
                or research.get("kind") != "opportunity_free_research_brief"):
            return False
        evidence_id = research.get("evidence_id")
        if not isinstance(evidence_id, str) or not re.fullmatch(r"oe_[0-9a-f]{32}", evidence_id):
            return False
        evidence = self._small_artifact(self.base / "evidence" / f"{evidence_id}.json")
        if (evidence is None or evidence.get("evidence_id") != evidence_id
                or evidence.get("kind") != "opportunity_evidence_brief"):
            return False
        opportunity = evidence.get("opportunity")
        if not isinstance(opportunity, dict):
            return False
        for key in ("opportunity_id", "type", "path", "symbol"):
            if opportunity.get(key) != cooldown.get(key):
                return False
        if (opportunity.get("opportunity_id") != data.get("opportunity_id")
                or (research.get("opportunity_id") is not None
                    and research.get("opportunity_id") != data.get("opportunity_id"))):
            return False
        relative = opportunity.get("path")
        baseline_sha = opportunity.get("source_sha256")
        if (not isinstance(relative, str) or not relative.startswith("src/sira/")
                or "\\" in relative or ".." in Path(relative).parts
                or not isinstance(baseline_sha, str)
                or not _FINGERPRINT_RE.fullmatch(baseline_sha)
                or promotion.get("changed_files") != [relative]):
            return False
        source = self.root / relative
        try:
            if (source.is_symlink() or not source.is_file()
                    or source.stat().st_size > MAX_ARTIFACT_BYTES
                    or not source.resolve().is_relative_to(self.root)):
                return False
            return hashlib.sha256(source.read_bytes()).hexdigest() == baseline_sha
        except (OSError, UnicodeError):
            return False

    def _event_reverted(self, event: dict[str, Any]) -> bool:
        handoff_id = event.get("source_handoff_id")
        if not isinstance(handoff_id, str) or not re.fullmatch(r"ohf_[0-9a-f]{32}", handoff_id):
            return False
        handoff = self._small_artifact(self.handoffs / f"{handoff_id}.json")
        return handoff is not None and self._proven_reverted_handoff(handoff, handoff_id)

    def record_source_reversion(self, handoff_id: str) -> bool:
        """Record a verified restoration, keeping the original promotion audit intact."""
        if not isinstance(handoff_id, str) or not re.fullmatch(r"ohf_[0-9a-f]{32}", handoff_id):
            raise TargetSelectionError("Invalid promotion handoff ID")
        handoff = self._small_artifact(self.handoffs / f"{handoff_id}.json")
        if handoff is None:
            raise TargetSelectionError("Promotion handoff not found")
        opportunity = handoff.get("cooldown")
        if not isinstance(opportunity, dict):
            raise TargetSelectionError("Promotion opportunity unavailable")
        key = opportunity_lineage_key(opportunity)
        lineages = self._load()
        entry = lineages.get(key)
        if entry is not None and any(
            event.get("source_handoff_id") == handoff_id
            and event.get("outcome") == "promotion_reverted"
            for event in entry["promotions"]
        ):
            return False
        if not self._proven_reverted_handoff(handoff, handoff_id):
            raise TargetSelectionError("Source restoration is not verified against promotion evidence")

        when = time.time()
        event = {
            "source_handoff_id": handoff_id,
            "opportunity_id": opportunity.get("opportunity_id"),
            "fingerprint": None,
            "outcome": "promotion_reverted",
            "restored_source_sha256": hashlib.sha256(
                (self.root / str(opportunity["path"])).read_bytes()
            ).hexdigest(),
            "attempted_at_epoch": float(when),
            "attempted_at": _iso_from_epoch(float(when)),
        }
        if entry is None:
            entry = {
                "type": opportunity.get("type"),
                "path": opportunity.get("path"),
                "symbol": opportunity.get("symbol"),
                "promotions": [],
            }
            lineages[key] = entry
        entry["promotions"].append(event)
        entry["promotions"].sort(key=lambda row: float(row["attempted_at_epoch"]), reverse=True)
        del entry["promotions"][MAX_PROMOTIONS_PER_LINEAGE:]
        self._save(lineages)
        return True

    def import_handoffs(self) -> int:
        """Import prior successful v1.0C-3d handoffs without executing them."""
        if not self.handoffs.is_dir() or self.handoffs.is_symlink():
            return 0
        imported = 0
        lineages = self._load()
        paths = sorted(self.handoffs.glob("ohf_*.json"))[:MAX_HANDOFF_IMPORTS]
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            try:
                if path.stat().st_size > MAX_ARTIFACT_BYTES:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (not isinstance(data, dict)
                    or data.get("schema_version") != 1
                    or data.get("kind") != "opportunity_writer_handoff"
                    or data.get("status") != "promoted"
                    or data.get("promotion_performed") is not True):
                continue
            if self._proven_reverted_handoff(data, path.stem):
                continue
            cooldown = data.get("cooldown")
            if not isinstance(cooldown, dict):
                continue
            when = cooldown.get("attempted_at_epoch")
            opportunity = {
                "opportunity_id": cooldown.get("opportunity_id"),
                "fingerprint": None,
                "type": cooldown.get("type"),
                "path": cooldown.get("path"),
                "symbol": cooldown.get("symbol"),
            }
            try:
                key = opportunity_lineage_key(opportunity)
                if any(
                    event.get("source_handoff_id") == path.stem
                    and event.get("outcome") == "promotion_reverted"
                    for event in lineages.get(key, {}).get("promotions", [])
                ):
                    continue
                added = self.record_promotion(
                    opportunity,
                    outcome=str(cooldown.get("outcome") or data.get("outcome") or "promotion_committed"),
                    attempted_at_epoch=when,
                    source_handoff_id=data.get("handoff_id") if isinstance(data.get("handoff_id"), str) else path.stem,
                )
            except TargetSelectionError:
                continue
            imported += int(added)
        return imported

    def state(self, opportunity: dict[str, Any], *, now_epoch: float) -> dict[str, Any]:
        if not isinstance(now_epoch, (int, float)) or isinstance(now_epoch, bool) or now_epoch < 0:
            raise TargetSelectionError("now_epoch must be a non-negative number")
        key = opportunity_lineage_key(opportunity)
        entry = self._load().get(key)
        if entry is None:
            return {
                "lineage_key": key,
                "eligible": True,
                "last_promotion_at": None,
                "remaining_seconds": 0,
                "recent_promotion_count": 0,
                "priority_penalty": 0,
            }
        reversed_handoffs = {
            event.get("source_handoff_id")
            for event in entry.get("promotions", [])
            if event.get("outcome") == "promotion_reverted"
        }
        successful = [
            event for event in entry.get("promotions", [])
            if event.get("outcome") == "promotion_committed"
            and float(event.get("attempted_at_epoch", 0)) <= float(now_epoch)
            and event.get("source_handoff_id") not in reversed_handoffs
            and not self._event_reverted(event)
        ]
        successful.sort(key=lambda row: float(row["attempted_at_epoch"]), reverse=True)
        latest = successful[0] if successful else None
        remaining = 0
        if latest is not None:
            elapsed = max(0.0, float(now_epoch) - float(latest["attempted_at_epoch"]))
            remaining = max(0, int(PROMOTION_SUPPRESSION_SECONDS - elapsed))
        cutoff = float(now_epoch) - DIMINISHING_WINDOW_SECONDS
        recent = [event for event in successful if float(event["attempted_at_epoch"]) >= cutoff]
        penalty = min(MAX_PRIORITY_PENALTY, len(recent) * PENALTY_PER_RECENT_PROMOTION)
        return {
            "lineage_key": key,
            "eligible": remaining == 0,
            "last_promotion_at": latest.get("attempted_at") if latest else None,
            "remaining_seconds": remaining,
            "recent_promotion_count": len(recent),
            "priority_penalty": penalty,
        }


def _memory_candidates(root: Path, limit: int) -> list[dict[str, Any]]:
    return MemoryStore(root).improvement_candidates(limit)


def _opportunity_discovery(root: Path, limit: int, now_epoch: float) -> dict[str, Any]:
    # Readiness and lineage are checked after discovery. A top-ranked batch
    # can contain only unmapped targets, hiding lower-ranked ready work.
    pool_limit = min(MAX_DISCOVERY_LIMIT, limit * 4)
    report = discover_opportunities(root, limit=pool_limit, now_epoch=now_epoch)
    # Owner-enabled breaker: stop spending model requests on an opportunity type that
    # keeps failing. No-op unless the owner policy enables it.
    from .engineering_failure_breaker import filter_opportunities
    return filter_opportunities(root, report, now_epoch=now_epoch)


def _knowledge_revalidation_candidates(root: Path, limit: int, now_epoch: float) -> list[dict[str, Any]]:
    return knowledge_revalidation_targets(root, limit=limit, now_epoch=now_epoch)


def _learning_goal_candidates(root: Path, limit: int, now_epoch: float) -> list[dict[str, Any]]:
    from .learning_goal_adaptive import preview_next_question
    return [
        {
            "target_kind": "learning_goal",
            "priority_class": 1,
            "priority_class_name": "owner_learning_goal",
            "learning_goal_id": row["goal_id"],
            "topic": row["topic"],
            "priority": row["priority"],
            "public_research_allowed": row["public_research_allowed"],
            "effective_priority_score": 10000 if row["priority"] == "high" else 9000,
            "created_at": row["created_at"],
            "paid_spending": False,
            "authority_granted": False,
            "promotion_authorized": False,
        }
        for row in LearningGoalStore(root).candidates(now_epoch=now_epoch, limit=limit)
        if preview_next_question(root, row, now_epoch=now_epoch) is not None
    ]


def _memory_target(root: Path, item: dict[str, Any]) -> dict[str, Any]:
    category = item.get("category")
    benchmark_evidence = benchmark_evidence_for_target(root, item)
    recurring_benchmark = (
        category == "benchmark_regression"
        and benchmark_evidence.get("classification") == "recurring_current_regression"
    )
    priority_class = 0 if category == "code_test" or recurring_benchmark else 1
    base_score = int(item.get("priority_score") or 0)
    benchmark_boost = (
        int(benchmark_evidence.get("priority_boost") or 0)
        if recurring_benchmark
        else 0
    )
    return {
        "target_kind": "memory",
        "priority_class": priority_class,
        "priority_class_name": (
            "failure_regression" if priority_class == 0 else "unresolved_weakness"
        ),
        **item,
        "raw_priority_score": base_score,
        "benchmark_priority_boost": benchmark_boost,
        "effective_priority_score": base_score + benchmark_boost,
        "benchmark_evidence": benchmark_evidence,
        "runtime_feedback": runtime_evaluator_feedback_summary(root, item),
    }


def _opportunity_target(
    root: Path,
    item: dict[str, Any],
    lineage: dict[str, Any],
) -> dict[str, Any]:
    raw_score = int(item.get("priority_score") or 0)
    penalty = int(lineage.get("priority_penalty") or 0)
    return {
        "target_kind": "opportunity",
        "priority_class": 3,
        "priority_class_name": "proactive_opportunity",
        **item,
        "raw_priority_score": raw_score,
        "effective_priority_score": max(0, raw_score - penalty),
        "lineage": lineage,
        "benchmark_evidence": benchmark_evidence_for_target(root, item),
        "runtime_feedback": runtime_evaluator_feedback_summary(root, item),
    }


def select_autonomous_target(
    root: Path,
    *,
    now_epoch: float | None = None,
    opportunity_limit: int = 20,
    memory_limit: int = 20,
    knowledge_limit: int = 10,
    learning_goal_limit: int = 10,
    memory_candidates_fn: Callable[[Path, int], list[dict[str, Any]]] = _memory_candidates,
    opportunity_discovery_fn: Callable[[Path, int, float], dict[str, Any]] = _opportunity_discovery,
    knowledge_candidates_fn: Callable[[Path, int, float], list[dict[str, Any]]] = _knowledge_revalidation_candidates,
    learning_goal_candidates_fn: Callable[[Path, int, float], list[dict[str, Any]]] = _learning_goal_candidates,
    excluded_target_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Select one local target with failures ordered before opportunities."""
    root = Path(root).resolve()
    when = time.time() if now_epoch is None else now_epoch
    if not isinstance(when, (int, float)) or isinstance(when, bool) or when < 0:
        raise TargetSelectionError("now_epoch must be a non-negative number")
    if type(opportunity_limit) is not int or not 1 <= opportunity_limit <= 20:
        raise TargetSelectionError("opportunity_limit must be 1..20")
    if type(memory_limit) is not int or not 1 <= memory_limit <= 20:
        raise TargetSelectionError("memory_limit must be 1..20")
    if type(knowledge_limit) is not int or not 1 <= knowledge_limit <= 20:
        raise TargetSelectionError("knowledge_limit must be 1..20")
    if type(learning_goal_limit) is not int or not 1 <= learning_goal_limit <= 20:
        raise TargetSelectionError("learning_goal_limit must be 1..20")
    excluded = set(excluded_target_ids or ())
    if any(not isinstance(item, str) or not item for item in excluded):
        raise TargetSelectionError("excluded_target_ids must contain non-empty strings")

    lineage_store = OpportunityLineageStore(root)
    imported = lineage_store.import_handoffs()
    memory_rows = memory_candidates_fn(root, memory_limit)
    if not isinstance(memory_rows, list):
        raise TargetSelectionError("memory candidate provider returned invalid data")
    discovery = opportunity_discovery_fn(root, opportunity_limit, float(when))
    if not isinstance(discovery, dict) or not isinstance(discovery.get("opportunities"), list):
        raise TargetSelectionError("opportunity discovery returned invalid data")
    knowledge_rows = knowledge_candidates_fn(root, knowledge_limit, float(when))
    if not isinstance(knowledge_rows, list):
        raise TargetSelectionError("knowledge candidate provider returned invalid data")
    if learning_goal_candidates_fn is _learning_goal_candidates:
        # Owner-enabled gap goals give the loop fresh work. No-op unless the owner
        # policy enables it; any failure is contained.
        from .gap_goal_generator import maybe_ensure_gap_goals
        maybe_ensure_gap_goals(root, now_epoch=float(when))
    goal_rows = learning_goal_candidates_fn(root, learning_goal_limit, float(when))
    if not isinstance(goal_rows, list):
        raise TargetSelectionError("learning goal candidate provider returned invalid data")

    candidates: list[dict[str, Any]] = []
    access_excluded: list[dict[str, Any]] = []
    for item in memory_rows:
        if isinstance(item, dict):
            candidate = _memory_target(root, item)
            if target_identity(candidate) in excluded:
                access_excluded.append(candidate)
            else:
                candidates.append(candidate)

    suppressed_lineage: list[dict[str, Any]] = []
    readiness_skipped: list[dict[str, str | None]] = []
    for item in discovery["opportunities"]:
        if not isinstance(item, dict):
            continue
        lineage = lineage_store.state(item, now_epoch=float(when))
        target = _opportunity_target(root, item, lineage)
        if lineage["eligible"]:
            if target_identity(target) in excluded:
                access_excluded.append(target)
            else:
                preview = opportunity_readiness_preview(root, item)
                if preview["ready"]:
                    candidates.append(target)
                else:
                    readiness_skipped.append({
                        "opportunity_id": item.get("opportunity_id"),
                        "path": item.get("path"),
                        "reason": preview["reason"],
                    })
        else:
            suppressed_lineage.append(target)

    for item in knowledge_rows:
        if not isinstance(item, dict) or item.get("target_kind") != "knowledge_revalidation":
            continue
        candidate = dict(item)
        try:
            identity = target_identity(candidate)
        except ValueError:
            continue
        if identity in excluded:
            access_excluded.append(candidate)
        else:
            candidates.append(candidate)

    for item in goal_rows:
        if not isinstance(item, dict) or item.get("target_kind") != "learning_goal":
            continue
        candidate = dict(item)
        try:
            identity = target_identity(candidate)
        except ValueError:
            continue
        if identity in excluded:
            access_excluded.append(candidate)
        else:
            candidates.append(candidate)

    candidates.sort(key=lambda row: (
        int(row.get("priority_class", 99)),
        -int(row.get("effective_priority_score") or 0),
        str(row.get("last_seen_at") or ""),
        str(row.get("path") or ""),
        str(row.get("symbol") or ""),
    ))
    target = candidates[0] if candidates else None
    selection_id = "ats_" + uuid4().hex
    report = {
        "schema_version": 1,
        "kind": "autonomous_target_selection",
        "policy_version": TARGET_POLICY_VERSION,
        "selection_id": selection_id,
        "created_at": utc_now(),
        "status": (
            "selected" if target is not None
            else "idle_access_blocked" if access_excluded
            else "idle_no_candidate"
        ),
        "selection_policy": (
            "recurring current-code benchmark regression and code-test failures first; then other eligible unresolved "
            "failure/rejection memory and active owner learning goals; then conflicted or stale verified "
            "knowledge; then proactive local opportunity; "
            "one-off or stale benchmark failures cannot "
            "gain the recurring-regression priority class; prior runtime evaluator feedback is reused as audit context "
            "only and never mutates ranking; recent successful same-lineage promotions are suppressed and older recent "
            "promotions receive a bounded diminishing-return penalty"
        ),
        "target": target,
        "alternatives": candidates[1:10],
        "access_excluded_count": len(access_excluded),
        "access_excluded_targets": [target_identity(row) for row in access_excluded[:10]],
        "memory_candidate_count": len(memory_rows),
        "opportunity_candidate_count": len(discovery["opportunities"]),
        "knowledge_revalidation_candidate_count": len(knowledge_rows),
        "learning_goal_candidate_count": len(goal_rows),
        "suppressed_lineage_count": len(suppressed_lineage),
        "suppressed_lineage": suppressed_lineage[:10],
        "readiness_skipped_count": len(readiness_skipped),
        "readiness_skipped": readiness_skipped[:10],
        "lineage_imported_promotions": imported,
        "opportunity_fingerprint_cooldown_suppressed": int(discovery.get("suppressed_cooldown_count") or 0),
        "recurring_benchmark_signal_count": sum(
            1 for row in candidates
            if isinstance(row.get("benchmark_evidence"), dict)
            and row["benchmark_evidence"].get("classification") == "recurring_current_regression"
        ),
        "benchmark_evidence_authoritative": False,
        "runtime_feedback_reused_count": sum(
            1 for row in candidates
            if isinstance(row.get("runtime_feedback"), dict)
            and int(row["runtime_feedback"].get("cycle_count") or 0) > 0
        ),
        "runtime_feedback_authoritative": False,
        "api_requests": 0,
        "paid_spending": False,
    }
    artifact = root / "improvements" / "targeting" / "selections" / f"{selection_id}.json"
    write_json(artifact, report)
    report["artifact"] = str(artifact)
    write_json(artifact, report)
    return report
