# SIRA v1.0D-1 Unified Autonomous Target Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic unified autonomous target selector plus stable opportunity lineage cooldown/penalty without changing the persistent worker yet.

**Architecture:** A new `autonomous_targeting.py` module combines existing memory improvement candidates with local opportunity discovery. Failure/rejection targets are assigned stricter priority classes than proactive opportunities. A bounded lineage store imports prior opportunity handoff artifacts and records stable type/path/symbol promotion history so a changed source fingerprint cannot immediately trigger repeated refactoring.

**Tech Stack:** Python standard library, unittest, existing SIRA memory/opportunity stores.

**Spec:** `docs/superpowers/specs/2026-09-17-unified-autonomous-loop-design.md`

## Global Constraints

- No network/model calls in v1.0D-1 target selection.
- No persistent `self on` runtime integration in this patch.
- Real benchmark/code-test regressions outrank all proactive opportunities.
- Other eligible unresolved failure/rejection memories outrank proactive opportunities.
- Successful same-lineage promotion suppresses that target for 24 hours even if its source fingerprint changes.
- After suppression, recent successful promotions apply only a bounded priority penalty, not a permanent ban.
- Existing handoff artifacts are imported locally and safely.
- Protected shell and current self-modification boundaries remain unchanged.

---

### Task 1: Unified target selection and lineage store

**Files:**
- Create: `src/sira/autonomous_targeting.py`
- Create: `tests/test_autonomous_targeting.py`

**Interfaces:**
- Consumes: `MemoryStore.improvement_candidates()`, `discover_opportunities()` and persisted v1.0C-3d handoff artifacts.
- Produces: `select_autonomous_target(root, *, now_epoch=None, opportunity_limit=10) -> dict[str, object]` and `OpportunityLineageStore`.

- [ ] Write failing tests proving regression memory outranks opportunity, ordinary unresolved memory outranks opportunity, opportunity fallback works with no memory, a recent successful same-lineage promotion suppresses a changed fingerprint, and an older promotion creates a diminishing-return penalty.
- [ ] Run `python -m unittest tests.test_autonomous_targeting -v` and confirm failure because `sira.autonomous_targeting` does not exist.
- [ ] Implement stable lineage keys, bounded artifact import, 24-hour promotion suppression, bounded rolling penalty, and deterministic target-class ordering.
- [ ] Re-run `python -m unittest tests.test_autonomous_targeting -v` and require all tests green.

### Task 2: Version/docs and regression verification

**Files:**
- Modify: `src/sira/__init__.py`
- Modify: `README.md`
- Verify: full project

**Interfaces:**
- Consumes: completed Task 1.
- Produces: SIRA version `1.0d1` and fresh offline verification evidence.

- [ ] Bump version to `1.0d1` and document that v1.0D-1 is scheduler/lineage only; persistent worker integration remains next stage.
- [ ] Run `python -m unittest discover -s tests -q` and require zero failures.
- [ ] Run `python sira.py benchmark --improvement` and require all cases pass with zero API requests.
- [ ] Run a local scheduler smoke test with synthetic handoff history and verify `api_requests=0`, `paid_spending=false`, and same-lineage suppression.
