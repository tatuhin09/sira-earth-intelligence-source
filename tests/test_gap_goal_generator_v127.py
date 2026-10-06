"""Gap-driven learning goals: bounded, auditable, owner-controlled."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.autonomous_targeting import select_autonomous_target
from sira.gap_goal_generator import (
    CHECK_INTERVAL_SECONDS, GAP_TOPICS, _load_registry, ensure_gap_goals, load_policy,
    maybe_ensure_gap_goals, write_policy,
)
from sira.learning_goals import LearningGoalStore, MAX_STUDY_STEPS, _valid_study_plan
from sira.self_model import CAPABILITIES

NOW = 2_000_000_000.0


def gap(capability, reason="missing_evidence", state="unverified", refs=()):
    return {"capability": capability, "reason": reason, "state": state,
            "evidence_refs": list(refs)}


def model(*gaps):
    return {"gaps": list(gaps)}


class GapGoalBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        write_policy(self.root, enabled=True, max_active=2, capability_cooldown_days=14)

    def run_gen(self, gaps, at=NOW, **kwargs):
        return ensure_gap_goals(self.root, now_epoch=at, model=model(*gaps), **kwargs)

    def registry(self):
        return _load_registry(self.root)


class GapGoalTests(GapGoalBase):
    def test_disabled_by_default_creates_nothing_and_never_reads_the_self_model(self):
        (self.root / "memory/gap_goals/policy.json").unlink()
        result = ensure_gap_goals(self.root, now_epoch=NOW, model=None)  # would crash if read
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.store.list(), [])

    def test_gap_becomes_a_public_goal_with_a_valid_study_plan_and_audit_trail(self):
        result = self.run_gen([gap("memory_retrieval", refs=["ref-a", "ref-b"])])
        self.assertEqual(result["status"], "created")
        row = self.store.get(result["created"][0]["goal_id"])
        self.assertTrue(row["public_research_allowed"])
        self.assertEqual(row["status"], "active")
        self.assertTrue(_valid_study_plan(row["study_plan"]))
        entry = self.registry()["goals"][0]
        self.assertEqual(entry["capability"], "memory_retrieval")
        self.assertEqual(entry["gap_reason"], "missing_evidence")
        self.assertEqual(entry["evidence_refs"], ["ref-a", "ref-b"])
        self.assertIn("why", entry)

    def test_effects_are_never_model_paid_authority_or_promotion(self):
        result = self.run_gen([gap("research")])
        self.assertEqual((result["model_requests"], result["api_requests"],
                          result["paid_requests"]), (0, 0, 0))
        for key in ("paid_spending", "authority_granted", "promotion_performed",
                    "skill_activated"):
            self.assertIs(result[key], False)

    def test_local_only_policy_creates_local_only_goals(self):
        write_policy(self.root, enabled=True, public_research_allowed=False)
        result = self.run_gen([gap("research")])
        self.assertFalse(self.store.get(result["created"][0]["goal_id"])["public_research_allowed"])

    def test_check_interval_throttles_and_force_bypasses(self):
        self.run_gen([gap("research")])
        again = self.run_gen([gap("planning")], at=NOW + CHECK_INTERVAL_SECONDS - 1)
        self.assertEqual(again["status"], "throttled")
        later = self.run_gen([gap("planning")], at=NOW + CHECK_INTERVAL_SECONDS)
        self.assertEqual(later["status"], "created")
        self.assertEqual(self.run_gen([gap("tool_use")], at=NOW + CHECK_INTERVAL_SECONDS + 1,
                                      force=True)["status"], "at_capacity")

    def test_max_active_is_respected(self):
        for index, capability in enumerate(("research", "planning", "testing")):
            result = self.run_gen([gap(capability)], at=NOW + index * 3600)
        self.assertEqual(result["status"], "at_capacity")
        self.assertEqual(len([g for g in self.registry()["goals"] if g["state"] == "active"]), 2)

    def test_degraded_gaps_come_first_in_the_given_order(self):
        result = self.run_gen([gap("failure_recovery", "repeated_failures", "degraded"),
                               gap("research")])
        self.assertEqual(result["created"][0]["capability"], "failure_recovery")

    def test_provider_gaps_map_to_provider_routing_and_unknown_gaps_are_ignored(self):
        self.assertEqual(self.run_gen([gap("something_new")])["status"], "no_eligible_gap")
        result = self.run_gen([gap("provider:doaj", "provider_cooldown", "degraded")],
                              at=NOW + 3600)
        self.assertEqual(result["created"][0]["capability"], "provider:doaj")
        self.assertEqual(self.store.get(result["created"][0]["goal_id"])["topic"],
                         GAP_TOPICS["provider_routing"][0])

    def test_owner_goal_with_the_same_topic_is_left_alone_and_skipped(self):
        owner = self.store.create(GAP_TOPICS["research"][0], priority="high",
                                  public_research_allowed=False)
        result = self.run_gen([gap("research")])
        self.assertEqual(result["status"], "no_eligible_gap")
        self.assertEqual(self.store.get(owner["goal_id"]), owner)
        self.assertEqual(self.registry()["goals"], [])

    def test_exhausted_goal_is_paused_and_capability_cooldown_applies(self):
        first = self.run_gen([gap("research")])
        goal_id = first["created"][0]["goal_id"]
        plan = self.store.get(goal_id)["study_plan"]
        clock = NOW + 1
        for _round in range(2):
            for index, step in enumerate(plan):
                row = self.store.get(goal_id)
                self.store.record_attempt(
                    goal_id, "sources_discovered_needs_verification", at_epoch=clock,
                    study_step_index=row["study_next_index"], research_focus=step)
                clock += 10
        second = self.run_gen([gap("research")], at=NOW + 7 * 86_400)
        self.assertEqual(second["retired"], [goal_id])
        self.assertEqual(self.store.get(goal_id)["status"], "paused")
        self.assertEqual(second["status"], "no_eligible_gap")  # topic exists and cooldown holds
        third = self.run_gen([gap("research")], at=NOW + 15 * 86_400)
        self.assertEqual(third["status"], "no_eligible_gap")   # paused topic is never recreated
        self.assertEqual(self.store.get(goal_id)["status"], "paused")
        self.assertEqual(self.registry()["goals"][0]["retired_reason"], "study_plan_exhausted")

    def test_paused_by_owner_frees_the_slot_and_records_why(self):
        first = self.run_gen([gap("research")])
        goal_id = first["created"][0]["goal_id"]
        self.store.set_status(goal_id, "paused")
        result = self.run_gen([gap("planning")], at=NOW + 2 * 3600)
        self.assertIn(goal_id, result["retired"])
        entry = [g for g in self.registry()["goals"] if g["goal_id"] == goal_id][0]
        self.assertEqual(entry["retired_reason"], "goal_removed_or_paused")

    def test_malformed_policy_or_registry_fails_closed(self):
        (self.root / "memory/gap_goals/policy.json").write_text("{bad")
        self.assertEqual(self.run_gen([gap("research")])["status"], "policy_invalid")
        write_policy(self.root, enabled=True)
        (self.root / "memory/gap_goals/registry.json").write_text("{bad")
        self.assertEqual(self.run_gen([gap("research")])["status"], "registry_invalid")
        self.assertEqual(self.store.list(), [])
        with self.assertRaises(ValueError):
            write_policy(self.root, enabled=True, max_active=99)

    def test_self_model_failure_is_contained(self):
        result = maybe_ensure_gap_goals(self.root, now_epoch=NOW)
        self.assertIn(result["status"], {"created", "no_eligible_gap", "self_model_unavailable",
                                         "gap_goal_error", "throttled"})

    def test_topic_table_is_complete_valid_and_unique(self):
        self.assertEqual(set(GAP_TOPICS), set(CAPABILITIES))
        topics = [topic for topic, _ in GAP_TOPICS.values()]
        self.assertEqual(len({t.casefold() for t in topics}), len(topics))
        for topic, plan in GAP_TOPICS.values():
            self.assertTrue(1 <= len(topic) <= 500)
            self.assertTrue(_valid_study_plan(plan) and len(plan) <= MAX_STUDY_STEPS)


class GapGoalSchedulingTests(GapGoalBase):
    def test_idle_selection_becomes_a_learning_goal_target_when_enabled(self):
        no_work = dict(memory_candidates_fn=lambda *_: [],
                       opportunity_discovery_fn=lambda *_: {"opportunities": []},
                       knowledge_candidates_fn=lambda *_: [])
        # default goal provider is used, so the generator runs; a real self-model supplies gaps
        before = select_autonomous_target(self.root, now_epoch=NOW, **no_work)
        self.assertEqual(before["status"], "selected")
        self.assertEqual(before["target"]["target_kind"], "learning_goal")
        goals = [g for g in self.store.list()]
        self.assertEqual(len(goals), 1)
        self.assertTrue(goals[0]["topic"] in {t for t, _ in GAP_TOPICS.values()})

    def test_idle_selection_is_unchanged_when_disabled(self):
        (self.root / "memory/gap_goals/policy.json").unlink()
        result = select_autonomous_target(
            self.root, now_epoch=NOW, memory_candidates_fn=lambda *_: [],
            opportunity_discovery_fn=lambda *_: {"opportunities": []},
            knowledge_candidates_fn=lambda *_: [])
        self.assertEqual(result["status"], "idle_no_candidate")
        self.assertEqual(self.store.list(), [])

    def test_injected_goal_provider_never_triggers_generation(self):
        result = select_autonomous_target(
            self.root, now_epoch=NOW, memory_candidates_fn=lambda *_: [],
            opportunity_discovery_fn=lambda *_: {"opportunities": []},
            knowledge_candidates_fn=lambda *_: [],
            learning_goal_candidates_fn=lambda *_: [])
        self.assertEqual(result["status"], "idle_no_candidate")
        self.assertEqual(self.store.list(), [])


if __name__ == "__main__":
    unittest.main()
