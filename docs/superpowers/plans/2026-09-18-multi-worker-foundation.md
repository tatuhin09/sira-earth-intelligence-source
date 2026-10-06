# SIRA v1.2 Multi-Worker Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded, persistent multi-worker coordinator without changing autonomous promotion authority.

**Architecture:** Two read-only workers may run concurrently in distinct local workspaces; a code worker may run only afterward and behind the global metered budget. Task state and artifacts are persisted, interrupted tasks are abandoned and reselected, and a single-owner lease prepares later promotion serialization.

**Tech Stack:** Python standard library (`concurrent.futures`, `threading`, atomic JSON persistence, `os.O_EXCL`), existing SIRA storage/runtime conventions.

**Spec:** `docs/superpowers/specs/2026-09-18-multi-worker-foundation-design.md`

## Global Constraints

- Maximum two concurrent read-only workers.
- No direct promotion from the coordinator.
- Protected shell remains unchanged.
- Code phase is serialized and budget-gated.
- Interrupted running tasks become `abandoned_reselect` and retain artifacts.
- Offline diagnostic must use zero API/model requests and paid spending must remain false.

---

### Task 1: Coordination contract and task persistence

**Files:**
- Create: `src/sira/worker_coordination.py`
- Create: `tests/test_worker_coordination.py`

**Interfaces:**
- Produces: `WorkerContext`, `WorkerTaskStore`, `MultiWorkerCoordinator`, `PromotionLease`, `recover_orphaned_worker_tasks`, `run_multi_worker_check`.

- [x] **Step 1: Write a failing contract test** proving the module/API does not yet exist.
- [x] **Step 2: Run the focused test and observe the expected failure.**
- [x] **Step 3: Add the minimal public coordination contract.**
- [x] **Step 4: Run the focused test and observe it pass.**

### Task 2: Bounded concurrency, serialization, and recovery

**Files:**
- Modify: `src/sira/worker_coordination.py`
- Modify: `tests/test_worker_coordination.py`

**Interfaces:**
- `MultiWorkerCoordinator.run_task(target, research_worker=..., verification_worker=..., code_worker=None, budget_checker=None) -> dict`
- `PromotionLease.acquire(owner_id) -> bool`
- `PromotionLease.release(owner_id) -> bool`

- [x] **Step 1: Add failing tests** for distinct concurrent read-only workspaces, code serialization/budget deferral, promotion lease exclusivity, and orphan recovery.
- [x] **Step 2: Run the focused tests and observe missing behavior.**
- [x] **Step 3: Implement the minimum coordination/persistence behavior.**
- [x] **Step 4: Reproduce the concurrent lost-update failure and identify read-modify-write races in task records.**
- [x] **Step 5: Add task-store locking and rerun the focused suite.**

### Task 3: Offline CLI diagnostic and release version

**Files:**
- Modify: `src/sira/cli.py`
- Modify: `src/sira/__init__.py`
- Modify: `tests/test_worker_coordination.py`

**Interfaces:**
- CLI: `python sira.py self workers-check`

- [x] **Step 1: Add a failing CLI test for `self workers-check`.**
- [x] **Step 2: Run it and observe argparse reject the missing command.**
- [x] **Step 3: Wire the diagnostic and set package version to `1.2`.**
- [x] **Step 4: Run focused and full regression tests.**
