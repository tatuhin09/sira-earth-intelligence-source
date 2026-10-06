# SIRA v0.9 Improvement Loop Design

## Goal

Add a deterministic, local foundation that turns unresolved failure/rejection memory into a prioritized improvement target, a bounded research hypothesis, and an isolated offline regression experiment without modifying or promoting SIRA's main source tree.

## Scope

v0.9 implements four stages: target selection, structured root-cause research, hypothesis persistence/deduplication, and isolated regression experiments. It does not autonomously edit source code, install dependencies, call paid services, or promote candidate code.

## Data flow

```text
memory/sira_memory.sqlite3
        ↓
improve plan
        ↓
priority-scored unresolved memory
        ↓
improve research <MEMORY_ID>
        ↓
improvement_hypothesis JSON + memory transition to researched
        ↓
improve experiment <HYPOTHESIS_ID>
        ↓
isolated candidate workspace
        ↓
fixed unit-test + capability benchmark commands
        ↓
validated / rejected / blocked result
        ↓
append-only memory transition + experiment artifact
```

## Target selection

Only `failure` or `rejection` memories with `observed` or `rejected` state are eligible. Priority is deterministic and based on bounded occurrence frequency plus category/kind weights. Resolved (`validated`/`superseded`) memories are excluded.

## Research and hypotheses

Research is local and deterministic in v0.9. It searches related memory, applies category-specific root-cause guidance, and produces a bounded hypothesis linked to one memory. No network/model call is required. Identical hypothesis fingerprints are reused instead of creating repeated failed ideas.

## Experiments

Experiments use a copy of `sira.py`, `src/`, `tests/`, `benchmarks/`, docs and public config examples under `improvements/workspaces/<experiment_id>/candidate/`. They never copy `.env`, `memory/`, `runs/`, `.cache/`, or Git metadata. Commands are fixed in code; no arbitrary shell command is accepted.

The current tree and candidate run the full unit test suite plus the capability-specific offline benchmark. A failed current baseline blocks the experiment. A candidate regression is rejected. A passing regression-check experiment can validate that a historical failure is already mitigated by the current implementation. Promotion is always false in v0.9.

## Memory lifecycle

v0.9 adds `experimenting` to the existing append-only state machine:

```text
observed → researched → experimenting → validated
                               └──────→ rejected
experimenting → researched (unhealthy baseline)
rejected → researched
validated → superseded
```

## Safety / integrity boundaries

- Main source tree is never modified by the experiment runner.
- Candidate workspaces omit secrets and historical state.
- Subprocess commands are fixed, use `shell=False`, and have timeouts.
- API credential environment variables are removed from experiment subprocesses.
- No network provider is called by the improvement benchmark.
- No candidate is merged/promoted in v0.9.
- Artifact IDs and file size/format are validated before loading.

## CLI

```bash
python sira.py improve plan
python sira.py improve research <MEMORY_ID>
python sira.py improve experiment <HYPOTHESIS_ID>
python sira.py improve status
python sira.py benchmark --improvement
```

## Limitations

v0.9 does not prove causal improvement from arbitrary code changes. Its regression-check mode can validate that current behavior passes the deterministic suite for a historical failure. Candidate source modification, metric-aware before/after comparison, promotion and rollback belong to later milestones.

A hypothesis that already produced a rejected experiment is not executed again unchanged. Re-research returns the prior hypothesis with `repeat_blocked: true`; a later stage must generate a materially different strategy before another experiment.
