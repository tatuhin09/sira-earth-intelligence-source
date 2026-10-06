"""Offline structural regression suite for the scholarly-paper flow."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .papers import Paper, PaperBatch, papers_run
from .storage import code_digest, write_json

CASES = Path(__file__).resolve().parents[2] / "benchmarks" / "paper_cases.json"


class FixturePaperProvider:
    name = "paper_fixture"
    cache_namespace = "paper-fixture-v1"

    def __init__(self, rows):
        normalized = []
        for row in rows:
            values = {**row, "authors": tuple(row["authors"])}
            if "providers" in values:
                values["providers"] = tuple(values["providers"])
            normalized.append(Paper(**values))
        self._papers = tuple(normalized)

    def search(self, query: str, max_results: int) -> PaperBatch:
        return PaperBatch(self._papers[:max_results], api_requests=0, rejected_papers=0)


def paper_benchmark(root: Path) -> tuple[Path, dict]:
    data = CASES.read_bytes()
    suite = json.loads(data)
    rows = []
    for case in suite["cases"]:
        provider = FixturePaperProvider(case["papers"])
        path = papers_run(root, case["query"], provider, Settings(root, max_results=3), use_cache=False)
        result = json.loads((path / "papers.json").read_text(encoding="utf-8"))
        ids = [paper["paper_id"] for paper in result["papers"]]
        published_dates = [paper.get("published_date") for paper in result["papers"]]
        providers = [paper.get("providers", [paper.get("provider", "unknown")])
                     for paper in result["papers"]]
        passed = (
            ids == case["expected_ids"]
            and result["status"] == case["expected_status"]
            and result["metrics"]["duplicates_removed"] == case["expected_duplicates_removed"]
            and result["metrics"]["api_requests"] == 0
            and published_dates == case.get("expected_published_dates", published_dates)
            and providers == case.get("expected_providers", providers)
        )
        rows.append({
            "case_id": case["id"],
            "passed": passed,
            "run_id": result["run_id"],
            "metrics": result["metrics"],
        })
    report = {
        "schema_version": 1,
        "suite_id": suite["suite_id"],
        "kind": suite["kind"],
        "dataset_sha256": hashlib.sha256(data).hexdigest(),
        "code_sha256": code_digest(),
        "passed": sum(row["passed"] for row in rows),
        "failed": sum(not row["passed"] for row in rows),
        "api_requests": sum(row["metrics"]["api_requests"] for row in rows),
        "cases": rows,
    }
    path = root / "runs" / ("benchmark_papers_" + uuid4().hex) / "benchmark.json"
    write_json(path, report)
    return path, report
