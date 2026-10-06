"""Offline benchmark for the multilingual teacher/verifier bridge."""
from __future__ import annotations

from pathlib import Path
import tempfile

from .access_requests import AccessRequestStore
from .language_semantic_bridge import bridge_multilingual_query, language_cost_guard
from .learning_consolidation import LearningConsolidationStore
from .providers.gemini_language import LanguageModelBatch
from .storage import RunStore, write_json


class _FakeLanguageModel:
    def __init__(self, *, accepted=True):
        self.accepted = accepted
        self.interpret_calls = 0
        self.verify_calls = 0

    def interpret(self, text, profile):
        self.interpret_calls += 1
        return LanguageModelBatch({
            "detected_language": "Banglish",
            "canonical_english": "I will run the code now; tell me if there is a problem.",
            "research_queries": ["run code detect problem"],
            "candidate_mappings": [
                {"token": "korbo", "meaning": "will do", "confidence": 0.95}
            ],
            "uncertainty": "low",
        }, 1, 20, 10)

    def verify(self, text, proposal):
        self.verify_calls += 1
        return LanguageModelBatch({
            "accepted": self.accepted,
            "semantic_equivalent": self.accepted,
            "intent_preserved": self.accepted,
            "verified_canonical_english": "I will run the code now; tell me if there is a problem.",
            "verified_research_queries": ["run code detect problem"],
            "mapping_verdicts": [
                {"token": "korbo", "meaning": "will do", "supported": self.accepted, "confidence": 0.94}
            ],
            "reason": "verified" if self.accepted else "meaning uncertain",
        }, 1, 15, 8)


def language_semantic_benchmark(root: Path):
    root = Path(root).resolve()
    cases = []

    def add(case_id, passed):
        cases.append({"case_id": case_id, "passed": bool(passed)})

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)

        guard = language_cost_guard(sandbox, environ={})
        add("zero_cost_guard_default_denied", not guard["allowed"])

        local = bridge_multilingual_query(
            sandbox,
            "Please run the code and test this memory module",
            allow_model=False,
        )
        add("english_stays_local", local["status"] == "local_only" and local["metrics"]["api_requests"] == 0)

        blocked_model = _FakeLanguageModel()
        blocked = bridge_multilingual_query(
            sandbox,
            "ami akhon code korbo amk bolo",
            allow_model=True,
            environ={"GEMINI_API_KEY": "configured"},
            model=blocked_model,
        )
        add("missing_free_confirmation_blocks_before_model",
            blocked["status"] == "blocked" and blocked_model.interpret_calls == 0)

        missing_key = bridge_multilingual_query(
            sandbox,
            "ami akhon code korbo amk bolo",
            allow_model=True,
            environ={"SIRA_GEMINI_FREE_TIER_CONFIRMED": "true"},
            model=_FakeLanguageModel(),
        )
        notices = AccessRequestStore(sandbox).pending_notifications()
        add("missing_key_creates_owner_access_notification",
            missing_key["status"] == "blocked"
            and missing_key["owner_action_required"] is True
            and len(notices) == 1)

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        env = {
            "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
            "GEMINI_API_KEY": "configured",
        }
        model = _FakeLanguageModel()
        first = bridge_multilingual_query(
            sandbox,
            "ami akhon code korbo amk bolo",
            allow_model=True,
            environ=env,
            use_cache=False,
            model=model,
        )
        add("teacher_and_verifier_accept_bounded_query",
            first["status"] == "completed"
            and first["metrics"]["api_requests"] == 2
            and first["mapping_evidence_recorded"] == 1)
        add("one_verified_run_does_not_consolidate",
            "korbo" not in LearningConsolidationStore(sandbox).active_lexicon())

        second = bridge_multilingual_query(
            sandbox,
            "ami pore korbo kintu amk bolo",
            allow_model=True,
            environ=env,
            use_cache=False,
            model=model,
        )
        add("second_distinct_verified_context_consolidates",
            second["status"] == "completed"
            and LearningConsolidationStore(sandbox).active_lexicon().get("korbo") == "will do")

        cache_model = _FakeLanguageModel()
        cached_first = bridge_multilingual_query(
            sandbox,
            "ami akhon korbo",
            allow_model=True,
            environ=env,
            use_cache=True,
            model=cache_model,
        )
        before = cache_model.interpret_calls + cache_model.verify_calls
        cached_second = bridge_multilingual_query(
            sandbox,
            "ami akhon korbo",
            allow_model=True,
            environ=env,
            use_cache=True,
            model=cache_model,
        )
        after = cache_model.interpret_calls + cache_model.verify_calls
        add("cache_avoids_repeat_model_and_learning",
            cached_first["status"] == "completed"
            and cached_second["metrics"]["cache_hit"] is True
            and before == after
            and cached_second["mapping_evidence_recorded"] == 0)

        rejected = bridge_multilingual_query(
            sandbox,
            "ami bujhi na eita",
            allow_model=True,
            environ=env,
            use_cache=False,
            model=_FakeLanguageModel(accepted=False),
        )
        add("verifier_rejection_records_no_learning",
            rejected["status"] == "verification_rejected"
            and rejected["mapping_evidence_recorded"] == 0)

        add("bridge_never_grants_authority_or_spending",
            first["paid_spending"] is False
            and first["authority_granted"] is False
            and first["promotion_authorized"] is False
            and first["billing_changes_performed"] is False)

    report = {
        "schema_version": 1,
        "kind": "language_semantic_benchmark",
        "suite_id": "sira-language-semantic-v1.7c-b",
        "passed": sum(bool(row["passed"]) for row in cases),
        "failed": sum(not bool(row["passed"]) for row in cases),
        "api_requests": 0,
        "cases": cases,
    }
    run = RunStore(root)
    path = run.path / "language-semantic-benchmark.json"
    write_json(path, report)
    return path, report
