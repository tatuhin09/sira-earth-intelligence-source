"""Conservative local strategy learning from audited memory outcomes.

This module is advisory. It cannot make a policy-blocked or unavailable
strategy eligible, cannot authorize spending, and never returns raw outcome
context or secret-bearing provenance.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

from .memory import MemoryStore


SCHEMA = "sira.strategy_decision.v1"
MIN_EVIDENCE_EVENTS = 2
MAX_ABS_ADJUSTMENT = 0.15
POSITIVE_OUTCOMES = {"application_succeeded", "retrieval_helped"}
NEGATIVE_OUTCOMES = {"application_failed", "retrieval_irrelevant"}
NEUTRAL_OUTCOMES = {"retrieval_used"}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


class StrategyLearner:
    """Rank already-defined strategies using bounded historical evidence.

    The caller owns capability definition, provider availability, policy gates,
    metering/budget rules, and final execution. Learning is only a bounded
    ranking adjustment over candidates that the caller supplied.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.memory = MemoryStore(self.root)

    @staticmethod
    def _validate_capability(capability: str) -> str:
        if not isinstance(capability, str):
            raise ValueError("Strategy capability must be a string")
        value = capability.strip()
        if not 1 <= len(value) <= 120:
            raise ValueError("Strategy capability must be 1..120 characters")
        return value

    @staticmethod
    def _normalize_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("Strategy candidates must be a non-empty list")

        normalized = []
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError("Strategy candidate must be a dictionary")

            strategy_id = candidate.get("strategy_id")
            if not isinstance(strategy_id, str) or not 1 <= len(strategy_id.strip()) <= 120:
                raise ValueError("Strategy ID must be 1..120 characters")
            strategy_id = strategy_id.strip()
            if strategy_id in seen:
                raise ValueError("Duplicate strategy ID")
            seen.add(strategy_id)

            base_score = candidate.get("base_score")
            if isinstance(base_score, bool) or not isinstance(base_score, (int, float)):
                raise ValueError("Strategy base_score must be numeric")
            base_score = float(base_score)
            if not 0.0 <= base_score <= 1.0:
                raise ValueError("Strategy base_score must be between 0 and 1")

            available = candidate.get("available")
            policy_allowed = candidate.get("policy_allowed")
            if type(available) is not bool or type(policy_allowed) is not bool:
                raise ValueError("Strategy availability/policy flags must be boolean")

            normalized.append({
                "strategy_id": strategy_id,
                "base_score": base_score,
                "available": available,
                "policy_allowed": policy_allowed,
                "metered": bool(candidate.get("metered", False)),
            })
        return normalized

    def _aggregate(self, capability: str, strategy_id: str) -> dict[str, Any]:
        db = self.memory.db_path
        if not db.is_file():
            return {
                "eligible_events": 0,
                "positive": 0,
                "negative": 0,
                "neutral": 0,
                "weighted_signal": 0.0,
                "contradicted_event_weight": 0.0,
            }

        positive = 0
        negative = 0
        neutral = 0
        weighted_signal = 0.0
        contradicted_weight = 0.0
        eligible_events = 0

        with closing(self.memory._connect()) as conn:
            rows = conn.execute(
                """SELECT o.outcome_type,o.weight,o.context_json,
                          m.real_occurrence_count,m.synthetic_occurrence_count,
                          m.contradiction_count
                   FROM memory_outcomes AS o
                   JOIN memories AS m ON m.memory_id=o.memory_id
                   ORDER BY o.created_at,o.outcome_id"""
            ).fetchall()

            import json
            for row in rows:
                if int(row["real_occurrence_count"] or 0) <= 0:
                    continue

                try:
                    context = json.loads(row["context_json"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(context, dict):
                    continue
                if context.get("strategy_id") != strategy_id:
                    continue
                if context.get("capability") != capability:
                    continue

                outcome_type = str(row["outcome_type"])
                if (
                    outcome_type not in POSITIVE_OUTCOMES
                    and outcome_type not in NEGATIVE_OUTCOMES
                    and outcome_type not in NEUTRAL_OUTCOMES
                ):
                    continue

                raw_weight = max(0.0, min(4.0, float(row["weight"] or 0.0)))
                contradiction_count = max(0, int(row["contradiction_count"] or 0))
                contradiction_factor = max(0.35, 1.0 - min(contradiction_count, 3) * 0.20)
                effective_weight = raw_weight * contradiction_factor

                if contradiction_count:
                    contradicted_weight += max(0.0, raw_weight - effective_weight)

                eligible_events += 1
                if outcome_type in POSITIVE_OUTCOMES:
                    positive += 1
                    weighted_signal += effective_weight
                elif outcome_type in NEGATIVE_OUTCOMES:
                    negative += 1
                    weighted_signal -= effective_weight
                else:
                    neutral += 1

        return {
            "eligible_events": eligible_events,
            "positive": positive,
            "negative": negative,
            "neutral": neutral,
            "weighted_signal": round(weighted_signal, 6),
            "contradicted_event_weight": round(contradicted_weight, 6),
        }

    @staticmethod
    def _learned_adjustment(evidence: dict[str, Any]) -> float:
        events = int(evidence["eligible_events"])
        if events < MIN_EVIDENCE_EVENTS:
            return 0.0

        signal = float(evidence["weighted_signal"])
        # Require repeated evidence, then scale conservatively. Four clean
        # same-direction events reaches the cap; mixed evidence cancels.
        adjustment = (signal / max(4.0, float(events))) * MAX_ABS_ADJUSTMENT
        return max(-MAX_ABS_ADJUSTMENT, min(MAX_ABS_ADJUSTMENT, adjustment))

    def rank(
        self,
        capability: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        capability = self._validate_capability(capability)
        normalized = self._normalize_candidates(candidates)

        ranking = []
        learning_event_count = 0
        used_learning = False

        for candidate in normalized:
            evidence = self._aggregate(capability, candidate["strategy_id"])
            learning_event_count += int(evidence["eligible_events"])
            adjustment = self._learned_adjustment(evidence)
            if adjustment != 0.0:
                used_learning = True

            eligible = bool(candidate["available"] and candidate["policy_allowed"])
            reasons = []
            if not candidate["available"]:
                reasons.append("unavailable")
            if not candidate["policy_allowed"]:
                reasons.append("policy_blocked")
            if int(evidence["eligible_events"]) < MIN_EVIDENCE_EVENTS:
                reasons.append("insufficient_evidence")
            elif adjustment > 0:
                reasons.append("historical_success_boost")
            elif adjustment < 0:
                reasons.append("historical_failure_penalty")
            else:
                reasons.append("mixed_or_neutral_history")

            final_score = _clamp(candidate["base_score"] + adjustment)
            ranking.append({
                "strategy_id": candidate["strategy_id"],
                "eligible": eligible,
                "base_score": candidate["base_score"],
                "learned_adjustment": round(adjustment, 6),
                "final_score": round(final_score, 6),
                "metered": candidate["metered"],
                "reasons": reasons,
                "evidence": evidence,
            })

        ranking.sort(
            key=lambda row: (
                1 if row["eligible"] else 0,
                row["final_score"],
                # For deterministic no-history ties, lexical ascending wins
                # after reverse=False below by using raw ID.
                row["strategy_id"],
            ),
            reverse=True,
        )

        eligible_rows = [row for row in ranking if row["eligible"]]
        if not eligible_rows:
            recommended = None
        else:
            best_score = max(row["final_score"] for row in eligible_rows)
            best = [row for row in eligible_rows if row["final_score"] == best_score]
            # Deterministic policy-safe fallback: lexical ascending.
            recommended = sorted(best, key=lambda row: row["strategy_id"])[0]["strategy_id"]

        return {
            "schema": SCHEMA,
            "capability": capability,
            "recommended_strategy_id": recommended,
            "used_learning": used_learning,
            "learning_event_count": learning_event_count,
            "ranking": ranking,
        }
