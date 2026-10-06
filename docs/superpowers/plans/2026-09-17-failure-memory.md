# Failure Learning + Persistent Experiment Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic, provenance-linked local learning memory for SIRA run outcomes.

**Architecture:** Extract normalized observations from existing JSON run artifacts and store them in a WAL-mode SQLite database with deduplicated fingerprints, append-only occurrences/transitions, and automatic validated-backup recovery. Keep all operations offline and expose bounded CLI/query/benchmark interfaces.

**Tech Stack:** Python standard library (`sqlite3`, `hashlib`, `json`, `pathlib`, `shutil`), existing SIRA CLI/storage patterns, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-17-failure-memory-design.md`

## Global Constraints
- Standard-library only; zero paid/API/network dependency.
- Existing run artifacts remain canonical provenance and are never rewritten.
- Memory database uses SQLite WAL mode, schema version 1, bounded fields, and a valid latest backup.
- Corrupt database is quarantined before restoring the latest valid backup.
- Existing commands and 99 baseline tests must remain compatible.

---

### Task 1: Memory store and learning extraction
**Files:** Create `src/sira/memory.py`; Test `tests/test_memory.py`.
**Interfaces:** `MemoryStore(root)`, `learn_run(root, run_id)`, `learn_all(root)`; methods `search`, `show`, `stats`, `transition`.
- [ ] Write tests for deduplication, provenance occurrences, success/rejection extraction, WAL, recovery, transitions and traversal rejection.
- [ ] Run `python -m unittest discover -s tests -p 'test_memory.py' -v` and verify RED because `sira.memory` does not exist.
- [ ] Implement schema initialization, integrity check/recovery, backup, stable fingerprints, artifact discovery/extraction, and bounded query methods.
- [ ] Re-run the memory tests and verify GREEN.

### Task 2: CLI integration
**Files:** Modify `src/sira/cli.py`, `src/sira/__init__.py`; Test `tests/test_memory.py`.
**Interfaces:** `learn <RUN_ID>|--all`, `memory search/show/stats`.
- [ ] Keep the existing CLI tests RED until commands exist.
- [ ] Add mutually exclusive learn target handling and JSON-only outputs; add nested memory subcommands.
- [ ] Set SIRA version to `0.8` and update CLI description.
- [ ] Run memory tests and verify GREEN.

### Task 3: Offline memory benchmark and docs
**Files:** Create `src/sira/memory_benchmark.py`, `benchmarks/memory_cases.json`, `docs/MEMORY.md`; Modify `src/sira/cli.py`, `README.md`; Test `tests/test_memory_benchmark.py`.
**Interfaces:** `memory_benchmark(root) -> (path, report)`; CLI `benchmark --memory`.
- [ ] Add a failing CLI benchmark test covering repeated rate-limit dedup, validated success, rejected claim, and prior-memory search; assert `api_requests == 0`.
- [ ] Implement deterministic fixture benchmark and persisted report under a new run directory.
- [ ] Document schema, CLI, status lifecycle, corruption recovery, and limitations.
- [ ] Run benchmark test and verify GREEN.

### Task 4: Full regression
- [ ] Run `python -m compileall -q src tests`.
- [ ] Run `python -m unittest discover -s tests -v`.
- [ ] Run retrieval, reading, synthesis, papers, paper-reading, and memory benchmarks; every suite must report zero failures and memory benchmark must use zero API requests.
- [ ] Build an atomic hash-guarded update package against v0.7 and apply it to a fresh v0.7 copy.
- [ ] Repeat the full verification on the freshly patched copy before delivery.
