# Opportunity Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic local opportunity discovery, ranking, fingerprinting, and cooldown state without network access or runtime-loop integration.

**Architecture:** A focused `opportunity.py` module scans bounded modifiable Python source, emits evidence-backed opportunities, persists scan artifacts, and keeps attempt cooldown history. The CLI exposes read-only discovery under `improve opportunities`; future runtime work can reuse `OpportunityStore.mark_attempt()`.

**Tech Stack:** Python standard library (`ast`, `hashlib`, `json`, `time`, `pathlib`), existing SIRA storage helpers and self-modification policy.

**Spec:** `docs/superpowers/specs/2026-09-17-opportunity-discovery-design.md`

## Global Constraints

- Zero network/API requests in discovery.
- Protected-shell paths are never autonomous opportunity targets.
- Discovery is bounded by file count and file size.
- Scanning does not start cooldown; attempts do.
- Main source tree is never modified by discovery.

---

### Task 1: Local scanner and stable ranking

**Files:**
- Create: `src/sira/opportunity.py`
- Test: `tests/test_opportunity.py`

**Interfaces:**
- Produces: `discover_opportunities(root, limit=5, cooldown_seconds=21600, now_epoch=None) -> dict`
- Produces: `OpportunityStore(root).mark_attempt(opportunity, outcome, attempted_at_epoch=None) -> dict`

- [ ] Write failing tests for bounded scanning, protected-path exclusion, deterministic ranking, stable fingerprints, and zero API requests.
- [ ] Run `python -m unittest tests.test_opportunity -v` and verify failure because the module/API does not exist.
- [ ] Implement the minimal scanner/store required by those tests.
- [ ] Re-run the focused tests and keep them green.

### Task 2: Cooldown behavior and source-change refresh

**Files:**
- Modify: `src/sira/opportunity.py`
- Modify: `tests/test_opportunity.py`

- [ ] Add failing tests showing an attempted fingerprint is suppressed during cooldown and becomes eligible after cooldown.
- [ ] Add a failing test showing changing source content changes the fingerprint and bypasses the old fingerprint's cooldown.
- [ ] Implement attempt history validation and cooldown filtering.
- [ ] Run focused tests.

### Task 3: CLI and documentation

**Files:**
- Modify: `src/sira/cli.py`
- Modify: `src/sira/__init__.py`
- Modify: `README.md`
- Modify: `tests/test_opportunity.py`

- [ ] Add a failing CLI test for `improve opportunities` JSON output.
- [ ] Add the CLI subcommand and version bump.
- [ ] Document stage scope and the local/free-first resource policy.
- [ ] Run focused tests, full unit suite, and `python sira.py benchmark --improvement`.
