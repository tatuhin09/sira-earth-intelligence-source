"""Fixed offline document replay. Does not measure live retrieval or truth."""
import hashlib
import json
from pathlib import Path

from .models import utc_now
from .reading import Document, ExtractBatch, read_run
from .storage import RunStore, code_digest, write_json

CASES = Path(__file__).resolve().parents[2] / "benchmarks" / "reading_cases.json"


class FixtureReader:
    name = "fixture_reader"

    def __init__(self, texts):
        self.texts = texts
        self.cache_namespace = "reading-fixture-" + hashlib.sha256(
            json.dumps(texts, sort_keys=True).encode()).hexdigest()

    def read(self, urls):
        documents = tuple(Document(url, self.texts[url], utc_now()) for url in urls if url in self.texts)
        failures = {url: "extraction_failed" for url in urls if url not in self.texts}
        return ExtractBatch(documents, failures)


def reading_benchmark(root):
    raw = CASES.read_bytes()
    suite = json.loads(raw)
    rows = []
    for case in suite["cases"]:
        parent = RunStore(root)
        parent.event("fixture_parent_created", case_id=case["id"])
        write_json(parent.path / "result.json", {"schema_version": 1, "run_id": parent.run_id,
                   "question": case["question"], "sources": case["sources"], "provider": "fixture"})
        path = read_run(root, parent.run_id, FixtureReader(case["texts"]), use_cache=False)
        result = json.loads((path / "evidence.json").read_text(encoding="utf-8"))
        actual_quotes = [q["quote"] for q in result["evidence"]]
        expected = case["expected_quote"]
        ok = (result["status"] == case["expected_status"]
              and result["metrics"]["sources_read"] == case["expected_read"]
              and (expected in actual_quotes if expected else not actual_quotes)
              and result["metrics"]["api_requests"] == 0
              and "<script>" not in (path / "report.html").read_text(encoding="utf-8"))
        rows.append({"case_id": case["id"], "passed": ok, "run_id": result["run_id"],
                     "metrics": result["metrics"]})
    report = {"schema_version": 1, "suite_id": suite["suite_id"], "kind": suite["kind"],
              "dataset_sha256": hashlib.sha256(raw).hexdigest(), "code_sha256": code_digest(),
              "passed": sum(r["passed"] for r in rows), "failed": sum(not r["passed"] for r in rows),
              "api_requests": sum(r["metrics"]["api_requests"] for r in rows), "cases": rows}
    store = RunStore(root)
    path = store.path / "reading-benchmark.json"
    write_json(path, report)
    store.event("benchmark_finished", passed=report["passed"], failed=report["failed"])
    return path, report
