# SIRA v0.9 Improvement Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic failure → research → hypothesis → isolated experiment foundation over v0.8 memory.

**Architecture:** Extend `MemoryStore` with improvement candidate selection and an `experimenting` lifecycle state. Add `improvement.py` for persisted plans/hypotheses/experiments plus a fixed-command isolated runner. Add an offline benchmark and CLI commands while keeping promotion disabled.

**Tech Stack:** Python standard library, SQLite, unittest, subprocess with fixed argv, JSON artifacts.

**Spec:** `docs/superpowers/specs/2026-09-17-improvement-loop-design.md`

## Global Constraints

- Zero paid-service requirement; the improvement benchmark performs zero API requests.
- Main source tree must not be edited by experiment execution.
- Candidate workspace must not copy `.env`, memory, runs, cache, or Git metadata.
- No arbitrary user shell command execution.
- No automatic promotion in v0.9.
- Existing tests/benchmarks must remain green.

---

### Task 1: Memory priority and lifecycle

**Files:** Modify `src/sira/memory.py`; test `tests/test_improvement.py`.

- [x] Write a failing test that repeated/high-impact unresolved failures outrank one-off low-information failures.
- [x] Run the test and verify the improvement module/API is missing.
- [x] Add deterministic `improvement_candidates()` scoring and `experimenting` memory transitions.
- [x] Run focused tests and verify green.

### Task 2: Improvement artifacts and hypothesis deduplication

**Files:** Create `src/sira/improvement.py`; test `tests/test_improvement.py`.

- [x] Write failing tests for persisted plan artifacts, research transition, hypothesis fingerprint reuse and invalid IDs.
- [x] Implement safe artifact directories, validated IDs/loaders, deterministic category guidance and related-memory lookup.
- [x] Verify identical research reuses one hypothesis instead of duplicating it.

### Task 3: Isolated experiment runner

**Files:** Modify `src/sira/improvement.py`; test `tests/test_improvement.py`.

- [x] Write failing tests for isolated candidate copy, validated historical mitigation, candidate regression rejection and no main-tree mutation.
- [x] Implement fixed subprocess evaluation with secret-stripped environment and bounded timeout.
- [x] Implement memory transitions from researched → experimenting → validated/rejected.
- [x] Verify no promotion occurs.

### Task 4: CLI and offline benchmark

**Files:** Modify `src/sira/cli.py`; create `src/sira/improvement_benchmark.py`, `benchmarks/improvement_cases.json`, `tests/test_improvement_benchmark.py`.

- [x] Add `improve plan|research|experiment|status` and `benchmark --improvement`.
- [x] Add four deterministic offline fixture cases and assert `api_requests: 0`.
- [x] Run full unit suite and every existing benchmark.

### Task 5: Documentation and packaging

**Files:** Modify `README.md`, `docs/MEMORY.md`; create this plan/spec.

- [x] Document state lifecycle, CLI, isolation and limitations.
- [ ] Apply the generated update to a fresh v0.8.1 baseline and rerun compile/tests/all benchmarks before delivery.
