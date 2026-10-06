"""Offline benchmark for Evolution v1.4A strategy planning."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from .evolution import StrategyEvolutionPlanner
from .storage import RunStore, write_json


def evolution_benchmark(root: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(root).resolve()
    dataset = json.loads(
        (root / "benchmarks/evolution_cases.json").read_text(encoding="utf-8")
    )
    expected = len(dataset.get("cases", []))
    passed = 0

    with tempfile.TemporaryDirectory() as tmp:
        bench_root = Path(tmp)
        opportunity = {
            "opportunity_id": "op_" + "1" * 32,
            "fingerprint": "2" * 64,
            "type": "complex_function",
            "path": "src/sira/example.py",
            "symbol": "example",
            "source_sha256": "3" * 64,
        }
        strategies = [
            {"strategy": "Extract cohesive phases.", "basis": "research", "citation_ids": ["R1"]},
            {"strategy": "Separate validation from execution.", "basis": "research", "citation_ids": ["R2"]},
            {"strategy": "Add characterization coverage first.", "basis": "research", "citation_ids": ["R3"]},
        ]

        # 1 deterministic first strategy
        planner = StrategyEvolutionPlanner(bench_root / "a")
        passed += int(planner.plan(opportunity, strategies)["selected_index"] == 0)

        # 2 rejected strategy -> alternative
        planner = StrategyEvolutionPlanner(bench_root / "b")
        first = planner.plan(opportunity, strategies)
        planner.record_outcome(opportunity, first["selected_strategy"],
                               outcome="rejected_evaluator2", promotion_performed=False)
        passed += int(planner.plan(opportunity, strategies)["selected_index"] == 1)

        # 3 duplicate dedup
        planner = StrategyEvolutionPlanner(bench_root / "c")
        duplicate = [strategies[0], dict(strategies[0]), *strategies[1:]]
        passed += int(planner.plan(opportunity, duplicate)["deduplicated_strategy_count"] == 1)

        # 4 exhausted
        planner = StrategyEvolutionPlanner(bench_root / "d")
        for _ in range(3):
            item = planner.plan(opportunity, strategies)
            planner.record_outcome(opportunity, item["selected_strategy"],
                                   outcome="rejected_evaluator2", promotion_performed=False)
        passed += int(planner.plan(opportunity, strategies)["status"] == "strategy_exhausted")

        # 5 recent success suppression
        planner = StrategyEvolutionPlanner(bench_root / "e")
        item = planner.plan(opportunity, strategies)
        planner.record_outcome(opportunity, item["selected_strategy"],
                               outcome="promotion_committed", promotion_performed=True)
        passed += int(planner.plan(opportunity, strategies)["status"] == "recent_success_suppressed")

        # 6 aggregate-only history
        planner = StrategyEvolutionPlanner(bench_root / "f")
        item = planner.plan(opportunity, strategies)
        planner.record_outcome(opportunity, item["selected_strategy"],
                               outcome="writer_error", promotion_performed=False)
        rendered = repr(planner.plan(opportunity, strategies)).casefold()
        passed += int("raw_error" not in rendered and "traceback" not in rendered and "secret" not in rendered)

    failed = max(0, expected - passed)
    report = {
        "schema_version": 1,
        "kind": "evolution_benchmark",
        "suite_id": dataset.get("suite_id"),
        "passed": passed,
        "failed": failed,
        "api_requests": 0,
    }
    run = RunStore(root)
    path = run.path / "evolution-benchmark.json"
    write_json(path, report)
    return path, report
