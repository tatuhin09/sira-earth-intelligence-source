from __future__ import annotations
from pathlib import Path
import tempfile
from .language_intelligence import analyze_language, unicode_tokens
from .learning_consolidation import LearningConsolidationStore
from .storage import RunStore, write_json

def language_learning_benchmark(root: Path):
    root = Path(root).resolve()
    cases = []
    def add(case_id, passed):
        cases.append({"case_id":case_id,"passed":bool(passed)})

    add("bengali_script_detected", analyze_language("আমি বাংলা লিখছি").language_label == "bn")
    b = analyze_language("ami akhon code korbo kono somossa hole amk bolo")
    add("banglish_detected", b.language_label == "bn-Latn-banglish" and b.needs_semantic_teacher)
    add("mixed_detected", analyze_language("আমি code run korbo").language_label == "bn-en-mixed")
    e = analyze_language("Please run the code and test this memory module")
    add("english_local_path", e.language_label == "en" and not e.needs_semantic_teacher)
    t = unicode_tokens("বাংলা স্মৃতি retrieval")
    add("unicode_tokens", "বাংলা" in t and "স্মৃতি" in t and "retrieval" in t)

    with tempfile.TemporaryDirectory() as tmp:
        store = LearningConsolidationStore(Path(tmp))
        store.record_language_mapping("korbo","will do",evidence_id="lang:one",
            confidence=.95,verifier="independent",verified=True)
        add("one_mapping_pending", store.consolidate_language_mapping("korbo").status == "pending")
        store.record_language_mapping("korbo","will do",evidence_id="lang:two",
            confidence=.91,verifier="independent",verified=True)
        add("repeated_mapping_consolidates",
            store.consolidate_language_mapping("korbo").status == "consolidated"
            and store.active_lexicon().get("korbo") == "will do")
        store.record_language_mapping("amr","my",evidence_id="lang:three",
            confidence=.95,verifier="independent",verified=True)
        store.record_language_mapping("amr","our",evidence_id="lang:four",
            confidence=.96,verifier="independent",verified=True)
        add("mapping_conflict_blocks", store.consolidate_language_mapping("amr").status == "blocked")

        steps=("Classify failure.","Run reproducing test.","Patch candidate.","Run regression.")
        for i in range(2):
            store.record_skill_evidence("debug.python.test","Debug Python test",steps,
                evidence_id=f"skill:pre:{i}",succeeded=True,confidence=.9,source_kind="runtime")
        add("skill_needs_three_successes", store.consolidate_skill("debug.python.test").status == "pending")
        store.record_skill_evidence("debug.python.test","Debug Python test",steps,
            evidence_id="skill:pre:2",succeeded=True,confidence=.92,source_kind="runtime")
        decision=store.consolidate_skill("debug.python.test")
        add("skill_consolidates_without_authority",
            decision.status=="consolidated" and decision.to_dict()["authority_granted"] is False)
        expanded=analyze_language("ami akhon korbo",learned_lexicon=store.active_lexicon())
        add("learned_mapping_expands_future_input",
            expanded.learned_gloss is not None and "will do" in expanded.learned_gloss)

    report={"schema_version":1,"kind":"language_learning_benchmark",
            "suite_id":"sira-language-learning-v1.7c-a",
            "passed":sum(x["passed"] for x in cases),
            "failed":sum(not x["passed"] for x in cases),
            "api_requests":0,"cases":cases}
    run=RunStore(root)
    path=run.path/"language-learning-benchmark.json"
    write_json(path,report)
    return path,report
