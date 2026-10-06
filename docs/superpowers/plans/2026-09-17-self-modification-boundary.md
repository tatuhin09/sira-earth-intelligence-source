# SIRA v1.0B-1 Self-Modification Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a hardened isolated candidate workspace and validated text-edit contract without permitting main-tree promotion.

**Architecture:** A new `self_modification.py` module owns candidate-copy policy, protected paths, path validation, text edit application, and manifest generation. `improvement.py` delegates candidate creation to that module while retaining the existing non-promoting experiment flow.

**Tech Stack:** Python standard library, unittest, existing SIRA storage/digest utilities.

**Spec:** `docs/superpowers/specs/2026-09-17-self-modification-boundary-design.md`

## Global Constraints
- No main-tree mutation or promotion in v1.0B-1.
- No `.env`, credentials, memory, runtime state, runs, cache, improvements history, update backups, or Git metadata in candidate copies.
- Autonomous edits are text-only, bounded, relative, non-symlink, and limited to approved roots.
- Protected shell paths are immutable to the autonomous writer.

---

### Task 1: Candidate workspace policy and writer contract
**Files:** Create `src/sira/self_modification.py`; Test `tests/test_self_modification.py`.
**Interfaces:** Produce `prepare_candidate_workspace(root, destination)` and `ValidatedCandidateEditor.apply_text_edits(candidate_root, edits)`.
- [ ] Write failing tests for sensitive-tree exclusion, protected-path rejection, traversal rejection, allowed text edit, and main-tree immutability.
- [ ] Run `python -m unittest tests.test_self_modification -v` and confirm RED because the module is missing.
- [ ] Implement minimal policy, manifest, copier, edit validator, and bounded text writer.
- [ ] Re-run the focused test file and confirm GREEN.

### Task 2: Integrate hardened candidate creation into experiments
**Files:** Modify `src/sira/improvement.py`; Test `tests/test_improvement.py`.
**Interfaces:** `run_experiment()` uses `prepare_candidate_workspace()` and persists candidate policy metadata while remaining non-promoting.
- [ ] Add/adjust experiment assertions for policy manifest and non-promotion.
- [ ] Run focused improvement tests and confirm the new assertion fails before integration.
- [ ] Replace the legacy candidate copier with the new boundary module and record manifest metadata.
- [ ] Run focused improvement tests and confirm GREEN.

### Task 3: Version/docs and full verification
**Files:** Modify `src/sira/__init__.py`; keep the design and plan documents above.
- [ ] Set version to `1.0b1`.
- [ ] Run full unit tests.
- [ ] Run `python sira.py benchmark --improvement` and verify 4/4, zero API requests.
- [ ] Verify a candidate workspace contains no `.env`, `memory/`, `runtime/`, `runs/`, `.git/`, or `improvements/` directory.
