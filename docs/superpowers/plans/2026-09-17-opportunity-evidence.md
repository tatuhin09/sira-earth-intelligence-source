# Opportunity Evidence Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn one persisted static opportunity into a zero-network, evidence-backed research brief before any autonomous writer handoff.

**Architecture:** `opportunity_evidence.py` resolves a persisted opportunity, verifies source freshness, extracts exact local context, maps known capability benchmarks, and scores evidence strength. The CLI exposes this as `improve evidence <OPPORTUNITY_ID>` and persists a bounded JSON artifact for later free-research and writer stages.

**Tech Stack:** Python standard library (`ast`, `hashlib`, `json`, `pathlib`), existing `MemoryStore`, `PROTECTED_PATHS`, and `write_json` helpers.

**Spec:** `docs/superpowers/specs/2026-09-17-opportunity-discovery-design.md`

## Global Constraints

- Zero network/model/API calls.
- No source-tree modification and no opportunity cooldown mutation.
- Stale source hashes fail closed.
- Synthetic learning-memory context remains excluded.
- Unknown benchmark mappings cannot become `research_ready`.

---

### Task 1: Evidence resolver and local context

**Files:**
- Create: `src/sira/opportunity_evidence.py`
- Test: `tests/test_opportunity_evidence.py`

**Interfaces:**
- Produces: `build_opportunity_evidence(root: Path, opportunity_id: str) -> dict`

- [x] Write failing tests for exact target extraction, callers, tests, stale-source rejection, and zero API requests.
- [x] Run focused tests and verify failure because the evidence module does not exist.
- [x] Implement persisted-opportunity lookup, source-integrity validation, caller/test discovery, and artifact persistence.
- [x] Re-run focused tests.

### Task 2: Memory, benchmark, and measurable gate

**Files:**
- Modify: `src/sira/opportunity_evidence.py`
- Modify: `tests/test_opportunity_evidence.py`

- [x] Add failing tests for synthetic-memory exclusion, explicit benchmark mapping, weak-evidence skip, and structural success criteria.
- [x] Implement local memory lookup, benchmark mapping, evidence scoring, and measurable criteria.
- [x] Run focused tests.

### Task 3: CLI and documentation

**Files:**
- Modify: `src/sira/cli.py`
- Modify: `src/sira/__init__.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-09-17-opportunity-discovery-design.md`
- Test: `tests/test_opportunity_evidence.py`

- [x] Add failing CLI coverage for `improve evidence`.
- [x] Add the CLI subcommand and version bump.
- [x] Document the evidence gate and zero-cost resource policy.
- [ ] Run full unit tests and the improvement benchmark.
