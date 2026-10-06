"""Offline structural synthesis regression. This does not measure factual truth."""
import hashlib
import json
from pathlib import Path

from .storage import RunStore, code_digest, write_json
from .synthesis import ModelBatch, answer_run


CASES = Path(__file__).resolve().parents[2] / "benchmarks" / "synthesis_cases.json"


class FixtureSynthesisModel:
    name = "fixture_synthesis"
    model_id = "fixture-v1"

    def __init__(self, case):
        self.case = case
        self.cache_namespace = "fixture-" + case["id"]

    def generate(self, task, payload, schema):
        return ModelBatch(self.case["proposal"] if task == "propose" else self.case["verdict"])


def synthesis_benchmark(root):
    raw = CASES.read_bytes()
    suite = json.loads(raw)
    rows = []
    for case in suite["cases"]:
        parent = RunStore(root)
        parent_result = {"schema_version": 1, "run_id": parent.run_id,
                         "question": case["question"], "sources": []}
        write_json(parent.path / "result.json", parent_result)
        parent_raw = (parent.path / "result.json").read_bytes()
        evidence = RunStore(root)
        document_dir = evidence.path / "documents"
        document_dir.mkdir()
        document = document_dir / "S1.txt"
        document.write_text(case["text"], encoding="utf-8")
        write_json(evidence.path / "evidence.json", {
            "schema_version": 1, "kind": "source_evidence", "run_id": evidence.run_id,
            "parent_run_id": parent.run_id,
            "parent_sha256": hashlib.sha256(parent_raw).hexdigest(),
            "question": case["question"], "status": "completed",
            "documents": [{"source_id": "S1", "url": "https://example.org/evidence",
                           "title": "Offline fixture", "status": "read", "trust": "untrusted",
                           "verification": "provider_text_retrieved", "text_path": "documents/S1.txt",
                           "content_sha256": hashlib.sha256(case["text"].encode()).hexdigest()}],
            "evidence": [], "metrics": {}})
        path = answer_run(root, evidence.run_id, FixtureSynthesisModel(case), use_cache=False)
        result = json.loads((path / "answer.json").read_text(encoding="utf-8"))
        html = (path / "answer.html").read_text(encoding="utf-8")
        ok = (result["status"] == case["expected_status"]
              and len(result["accepted_claims"]) == case["expected_accepted"]
              and result["metrics"]["api_requests"] == 0 and "<script>" not in html)
        rows.append({"case_id": case["id"], "passed": ok, "run_id": result["run_id"],
                     "status": result["status"], "accepted_claims": len(result["accepted_claims"])})
    report = {"schema_version": 1, "suite_id": suite["suite_id"], "kind": suite["kind"],
              "dataset_sha256": hashlib.sha256(raw).hexdigest(), "code_sha256": code_digest(),
              "passed": sum(x["passed"] for x in rows), "failed": sum(not x["passed"] for x in rows),
              "api_requests": 0, "cases": rows,
              "limitations": ["Offline fixtures do not measure factual accuracy.",
                              "Fixture verdicts do not evaluate live model quality."]}
    store = RunStore(root)
    path = store.path / "synthesis-benchmark.json"
    write_json(path, report)
    store.event("benchmark_finished", passed=report["passed"], failed=report["failed"])
    return path, report
