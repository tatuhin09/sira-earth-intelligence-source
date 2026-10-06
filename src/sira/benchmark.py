"""A small regression baseline, not a factual-accuracy evaluator."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .controller import research
from .providers.fixture import FixtureProvider
from .storage import code_digest, write_json

CASES = Path(__file__).resolve().parents[2] / "benchmarks" / "cases.json"


def benchmark(root: Path) -> tuple[Path, dict]:
    data = CASES.read_bytes()
    suite = json.loads(data)
    provider = FixtureProvider(CASES)
    rows = []
    for case in suite["cases"]:
        path = research(case["query"], provider, Settings(root), use_cache=False)
        result = json.loads((path / "result.json").read_text(encoding="utf-8"))
        matches = ([s["url"] for s in result["sources"]] == case["expected_urls"]
                   and result["status"] == case["expected_status"]
                   and result["metrics"]["api_requests"] == 0)
        rows.append({"case_id": case["id"], "passed": matches,
                     "run_id": result["run_id"], "metrics": result["metrics"]})
    report = {"schema_version": 1, "suite_id": suite["suite_id"], "kind": suite["kind"],
              "dataset_sha256": hashlib.sha256(data).hexdigest(), "code_sha256": code_digest(),
              "passed": sum(r["passed"] for r in rows),
              "failed": sum(not r["passed"] for r in rows),
              "api_requests": sum(r["metrics"]["api_requests"] for r in rows), "cases": rows}
    path = root / "runs" / ("benchmark_" + uuid4().hex) / "benchmark.json"
    write_json(path, report)
    return path, report
