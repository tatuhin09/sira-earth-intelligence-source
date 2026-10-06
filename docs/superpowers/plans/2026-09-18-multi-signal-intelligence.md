# SIRA v1.1 Multi-Signal Opportunity Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four deterministic opportunity signals with signal-specific evidence, research, and independently verified promotion goals.

**Architecture:** `opportunity_detectors.py` owns modifiable local detection only. `opportunity.py` turns detector signals into the existing fingerprinted opportunity contract. Evidence/research/handoff modules define signal-specific behavioral requirements, while protected `autonomous_promotion.py` independently recomputes measurable candidate goals without importing detector code.

**Tech Stack:** Python standard library (`ast`, `hashlib`, `pathlib`), existing SIRA opportunity/evidence/research/promotion contracts, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-18-multi-signal-intelligence-design.md`

## Global Constraints

- Discovery uses zero network/model/API calls.
- Protected-shell source is never scanned as an opportunity target.
- Existing complexity opportunity fingerprints remain stable.
- New signal writer handoff requires mapped behavioral evidence and a measurable protected-gate goal.
- Trusted goal verification must not import modifiable detector code.

---

### Task 1: Multi-signal detector registry

**Files:**
- Create: `src/sira/opportunity_detectors.py`
- Modify: `src/sira/opportunity.py`
- Test: `tests/test_multi_signal_intelligence.py`

- [x] Write failing tests for complexity, missing-test, weak-error, and duplicate-body discovery.
- [x] Verify the tests fail because the new signals do not exist.
- [x] Implement bounded deterministic detectors and integrate them with opportunity fingerprinting/ranking.
- [x] Preserve v1.0 complexity fingerprint semantics with a separate fingerprint policy version.
- [x] Verify focused tests pass.

### Task 2: Signal-specific evidence and research

**Files:**
- Modify: `src/sira/opportunity_evidence.py`
- Modify: `src/sira/opportunity_research.py`
- Modify: `src/sira/opportunity_handoff.py`
- Test: `tests/test_multi_signal_intelligence.py`

- [x] Write failing tests for missing-test evidence readiness, research query/relevance routing, and minimum-goal prompt direction.
- [x] Add signal-specific evidence requirements and measurable goal contracts.
- [x] Add signal-specific research queries, relevance terms, and strategies.
- [x] Render both maximum and minimum goal directions in writer hypotheses.
- [x] Verify focused tests pass.

### Task 3: Independent trusted goal verification

**Files:**
- Modify: `src/sira/autonomous_promotion.py`
- Test: `tests/test_multi_signal_intelligence.py`

- [x] Write failing tests for test-call, weak-handler, duplicate-body, and import-provenance behavior.
- [x] Implement independent protected-gate metric recomputation without importing detector code.
- [x] Require exact baseline agreement and one explicit min/max bound before candidate evaluation.
- [x] Verify focused tests and the full unit suite pass.

### Task 4: Regression verification and documentation

**Files:**
- Modify: `README.md`
- Modify: `src/sira/__init__.py`

- [x] Document v1.0D reliability and v1.1 multi-signal behavior.
- [x] Set SIRA package version to `1.1` while leaving runtime protocol stage `1.0D-4` unchanged.
- [x] Run all unit tests.
- [x] Run retrieval, reading, synthesis, papers, paper-reading, memory, and improvement benchmarks and require zero failures.
