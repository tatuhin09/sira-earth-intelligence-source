# SIRA v1.0B-3a Evaluator 1 Final Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a protected non-promoting final gate that independently binds a verified candidate to the current main tree and fresh verification health.

**Architecture:** Evaluator 1 runs from trusted current/main code after Evaluator 2. It validates the candidate workspace manifest, current-main base digest, protected-shell identity, public-tree symlink freedom, supported Evaluator-2 policy, and non-regressing test/benchmark health, then emits an auditable authorization fingerprint without mutating main.

**Tech Stack:** Python standard library, unittest, existing SIRA candidate/evaluator contracts.

**Spec:** `docs/superpowers/specs/2026-09-17-evaluator1-final-gate-design.md`

## Global Constraints

- No main-tree mutation or promotion in v1.0B-3a.
- Evaluator 1 remains in the autonomous protected-path set.
- Candidate manifest must bind to the current main public-tree digest.
- A candidate cannot reduce verified test count or passing benchmark coverage.
- Authorization fingerprint is an audit checksum, not a secret signature.

---

### Task 1: Final gate contract

**Files:**
- Create: `src/sira/evaluator1.py`
- Create: `tests/test_evaluator1.py`
- Modify: `src/sira/__init__.py`

**Interfaces:**
- Consumes: `evaluate_candidate(...)` report, candidate workspace manifest, baseline and candidate health dictionaries.
- Produces: `evaluate_final_gate(main_root, candidate_root, evaluator2_report, baseline_health, candidate_health) -> dict[str, object]`.

- [ ] Write failing tests for verified allow, upstream reject, stale base, protected-shell tamper, symlink/corrupt integrity, and verification shrinkage.
- [ ] Run `python -m unittest tests.test_evaluator1 -v` and confirm failure because `sira.evaluator1` is missing.
- [ ] Implement the minimal deterministic gate and audit digests.
- [ ] Run `python -m unittest tests.test_evaluator1 -v` and confirm all new tests pass.
- [ ] Bump SIRA package version to `1.0b3a`.

### Task 2: Regression verification

**Files:**
- Verify all project files without additional behavior changes.

**Interfaces:**
- Consumes: completed Task 1.
- Produces: fresh full-suite and improvement benchmark evidence.

- [ ] Run `python -m unittest discover -s tests -q`.
- [ ] Run `python sira.py benchmark --improvement`.
- [ ] Run a live temporary candidate smoke test and require `promotion_allowed == True` and `promotion_performed == False`.
