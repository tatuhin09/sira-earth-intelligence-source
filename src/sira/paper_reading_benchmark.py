"""Offline regression suite for paper metadata -> evidence conversion."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .paper_reading import paper_read_run
from .storage import code_digest, write_json

CASES = Path(__file__).resolve().parents[2] / "benchmarks" / "paper_reading_cases.json"


class NoNetworkPdfReader:
    name = "paper_reading_fixture"
    cache_namespace = "paper-reading-fixture-v1"

    def read(self, urls):
        from .reading import ExtractBatch
        return ExtractBatch((), {url: "fixture_has_no_pdf" for url in urls}, api_requests=0, credits=0)


def _paper_parent(root: Path, case: dict) -> str:
    from .models import utc_now
    from .storage import RunStore
    run = RunStore(root)
    value = {
        "schema_version": 1,
        "sira_version": "fixture",
        "run_id": run.run_id,
        "code_sha256": "0" * 64,
        "recorded_at": utc_now(),
        "question": case["query"],
        "provider": "paper_fixture",
        "provider_config": "paper-fixture-v1",
        "settings": {"max_results": 3, "timeout_seconds": 20, "cache_ttl_seconds": 86400,
                     "use_cache": False},
        "status": "completed",
        "error": None,
        "output_kind": "scholarly_metadata_not_fact_checked",
        "papers": case["papers"],
        "metrics": {"paper_count": len(case["papers"]), "api_requests": 0},
    }
    write_json(run.path / "papers.json", value)
    return run.run_id


def paper_reading_benchmark(root: Path):
    raw = CASES.read_bytes()
    suite = json.loads(raw)
    rows = []
    for case in suite["cases"]:
        parent_id = _paper_parent(root, case)
        path = paper_read_run(root, parent_id, NoNetworkPdfReader(), use_cache=False)
        result = json.loads((path / "evidence.json").read_text(encoding="utf-8"))
        passed = (
            result["status"] == case["expected_status"]
            and result["metrics"]["sources_read"] == case["expected_sources_read"]
            and result["metrics"]["abstracts_used"] == case["expected_abstracts_used"]
            and result["metrics"]["api_requests"] == 0
            and result["parent_artifact"] == "papers.json"
        )
        rows.append({"case_id": case["id"], "passed": passed,
                     "run_id": result["run_id"], "metrics": result["metrics"]})
    report = {
        "schema_version": 1,
        "suite_id": suite["suite_id"],
        "kind": suite["kind"],
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "code_sha256": code_digest(),
        "passed": sum(row["passed"] for row in rows),
        "failed": sum(not row["passed"] for row in rows),
        "api_requests": sum((row["metrics"]["api_requests"] or 0) for row in rows),
        "cases": rows,
    }
    path = root / "runs" / ("benchmark_paper_reading_" + uuid4().hex) / "benchmark.json"
    write_json(path, report)
    return path, report
