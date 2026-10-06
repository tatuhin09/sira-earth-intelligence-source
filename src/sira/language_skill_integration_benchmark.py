"""Offline v1.7C-C benchmark for multilingual query + skill reuse integration."""
from __future__ import annotations
from pathlib import Path
import tempfile
from .language_semantic_bridge import bridge_multilingual_query
from .providers.gemini_language import LanguageModelBatch
from .skill_runtime import record_verified_handoff_skill, skill_context_for_opportunity
from .storage import RunStore, write_json

class _FakeLanguageModel:
    def __init__(self): self.calls = 0
    def interpret(self, text, profile):
        self.calls += 1
        return LanguageModelBatch({
            "detected_language":"Banglish",
            "canonical_english":"How can I improve test reliability?",
            "research_queries":["software testing reliability best practices"],
            "candidate_mappings":[],"uncertainty":"low"},1,10,5)
    def verify(self, text, proposal):
        self.calls += 1
        return LanguageModelBatch({
            "accepted":True,"semantic_equivalent":True,"intent_preserved":True,
            "verified_canonical_english":"How can I improve test reliability?",
            "verified_research_queries":["software testing reliability best practices"],
            "mapping_verdicts":[],"reason":"verified"},1,8,4)

def _success(handoff_id: str):
    return {"handoff_id":handoff_id,"status":"promoted","outcome":"promotion_committed",
            "promotion_performed":True,"main_tree_modified":True,
            "structural_check":{"decision":"pass"}}

def language_skill_integration_benchmark(root: Path):
    root = Path(root).resolve(); cases=[]
    def add(case_id, passed): cases.append({"case_id":case_id,"passed":bool(passed)})
    with tempfile.TemporaryDirectory() as tmp:
        sandbox=Path(tmp)
        english=bridge_multilingual_query(sandbox,"Please improve this test reliability",allow_model=False)
        add("english_query_stays_local", english["status"]=="local_only")
        fake=_FakeLanguageModel()
        blocked=bridge_multilingual_query(sandbox,"ami test reliability bhalo korte chai",allow_model=True,
            environ={"GEMINI_API_KEY":"configured"},model=fake)
        add("zero_cost_guard_precedes_teacher", blocked["status"]=="blocked" and fake.calls==0)
        accepted=bridge_multilingual_query(sandbox,"ami test reliability bhalo korte chai",allow_model=True,
            environ={"GEMINI_API_KEY":"configured","SIRA_GEMINI_FREE_TIER_CONFIRMED":"true"},
            use_cache=False,model=fake)
        add("verified_semantic_query_available", accepted["status"]=="completed" and
            accepted["research_queries"]==["software testing reliability best practices"])
        opportunity={"type":"complex_function"}
        first=record_verified_handoff_skill(sandbox,opportunity,_success("ohf_"+"1"*32))
        add("one_promotion_is_not_a_skill", first["consolidation"]["status"]=="pending" and
            skill_context_for_opportunity(sandbox,opportunity) is None)
        second=record_verified_handoff_skill(sandbox,opportunity,_success("ohf_"+"2"*32))
        add("two_promotions_still_pending", second["consolidation"]["status"]=="pending")
        third=record_verified_handoff_skill(sandbox,opportunity,_success("ohf_"+"3"*32))
        context=skill_context_for_opportunity(sandbox,opportunity)
        add("three_verified_promotions_consolidate", third["consolidation"]["status"]=="consolidated" and isinstance(context,dict))
        add("skill_reuse_is_advisory_only", context is not None and context["advisory_only"] is True and
            context["authority_granted"] is False and context["promotion_authorized"] is False)
        failed=record_verified_handoff_skill(sandbox,{"type":"weak_error_handling"},{
            "handoff_id":"ohf_"+"4"*32,"status":"rejected","outcome":"rejected_evaluator1",
            "promotion_performed":False,"main_tree_modified":False,"structural_check":{"decision":"pass"}})
        add("failed_handoff_never_becomes_skill_evidence", failed["status"]=="not_recorded" and failed["evidence_recorded"] is False)
        duplicate=record_verified_handoff_skill(sandbox,opportunity,_success("ohf_"+"3"*32))
        add("same_handoff_is_idempotent", duplicate["status"]=="duplicate_evidence")
        add("skill_learning_never_grants_spending_or_authority", third["authority_granted"] is False and
            third["promotion_authorized"] is False and accepted["paid_spending"] is False and accepted["billing_changes_performed"] is False)
    report={"schema_version":1,"kind":"language_skill_integration_benchmark",
            "suite_id":"sira-language-skill-integration-v1.7c-c",
            "passed":sum(bool(x["passed"]) for x in cases),"failed":sum(not bool(x["passed"]) for x in cases),
            "api_requests":0,"cases":cases}
    run=RunStore(root); path=run.path/"language-skill-integration-benchmark.json"; write_json(path,report)
    return path,report
