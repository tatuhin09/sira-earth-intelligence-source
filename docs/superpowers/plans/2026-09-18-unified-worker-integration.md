# SIRA v1.0D-2 Unified Worker Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route persistent `self on` cycles through the unified scheduler and the existing opportunity evidence/research/writer/promotion chain.

**Architecture:** Keep the existing memory cycle as the implementation for memory targets, but pin it with a scheduler-provided planner. Add a unified cycle wrapper for proactive opportunities with runtime ownership checkpoints between evidence, research, and writer handoff stages. Reuse all existing evaluator and transactional promotion components unchanged.

**Tech Stack:** Python standard library, unittest, existing SIRA runtime/targeting/opportunity modules.

**Spec:** `docs/superpowers/specs/2026-09-18-unified-worker-integration-design.md`

## Global Constraints

- `self off` must prevent a new writer/promotion transaction once observed.
- Failure/rejection priorities come only from the unified selector.
- Weak opportunity evidence/research must not invoke the writer.
- Protected shell and promotion gates remain unchanged.
- v1.0D-2 does not add new paid services or resource-scheduling policy.

---

### Task 1: Unified runtime cycle

**Files:**
- Modify: `src/sira/runtime.py`
- Create: `tests/test_unified_runtime.py`

**Interfaces:**
- Consumes: `select_autonomous_target`, `build_opportunity_evidence`, `research_opportunity_free`, `run_opportunity_writer_handoff`, `run_one_improvement_cycle`.
- Produces: `run_unified_improvement_cycle(root, generation, ...) -> dict[str, Any]`.

- [x] Write failing tests for opportunity routing, exact memory delegation, idle persistent behavior, and stop-before-writer behavior.
- [x] Verify tests fail because `run_unified_improvement_cycle` is absent.
- [x] Implement the minimal unified cycle and make it the default persistent-loop cycle runner.
- [x] Re-run focused runtime tests and require green.

### Task 2: Runtime audit/version/docs

**Files:**
- Modify: `src/sira/__init__.py`
- Modify: `src/sira/runtime.py`
- Modify: `tests/test_runtime.py`
- Modify: `README.md`

**Interfaces:**
- Produces: runtime stage `1.0D-2`, richer last-cycle summary, version `1.0d2`.

- [x] Update stage/version and last-cycle audit fields.
- [x] Run full unittest suite.
- [x] Run all offline benchmark suites and require zero failures/API requests.
