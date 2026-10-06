# SIRA local learning memory (v0.8)

SIRA v0.8 adds a deterministic local memory substrate for learning from prior run outcomes. It does **not** autonomously edit code yet; it records and retrieves structured experience that later self-improvement stages can use.

## Storage

The canonical evidence remains the existing JSON run artifacts under `runs/`. Derived learning memory is stored at:

```text
memory/sira_memory.sqlite3
memory/backups/sira_memory.latest.sqlite3
memory/corrupt/...
```

SQLite uses schema version 1, foreign keys, WAL mode, bounded busy timeouts and explicit indexes. Every mutating transaction creates a validated SQLite backup. If the main DB fails `PRAGMA quick_check`, SIRA quarantines it and restores the latest valid backup. If neither copy is valid, SIRA stops instead of silently discarding history.

## Learn one run

```bash
python sira.py learn <RUN_ID>
```

SIRA hashes the source artifact and stores each normalized observation with exact run/artifact provenance. Re-learning the same artifact is idempotent. Equivalent failures from different runs share one memory fingerprint but preserve separate occurrences.

## Learn existing local history

```bash
python sira.py learn --all
```

This scans compatible local run folders only. It performs no network or model calls.

## Search / inspect

```bash
python sira.py memory search "rate limit"
python sira.py memory show <MEMORY_ID>
python sira.py memory stats
```

Memory search is deliberately simple and deterministic in v0.8: all normalized query tokens must occur across the summary/classification/provider fields. A future planner can use the same Python API before launching new research.

## Memory states

- `observed`: a failure/rejection has been seen but not validated as a reusable solution.
- `researched`: the issue has been investigated.
- `validated`: a successful outcome or researched lesson has evidence supporting it.
- `rejected`: the lesson/hypothesis was found unsuitable.
- `superseded`: a newer lesson replaces it while the old history remains intact.

Transitions are append-only in the `transitions` table. SIRA does not delete old lessons to make its history look better.

## Categories

`network`, `rate_limit`, `bad_source`, `provider`, `parsing`, `verification`, `citation`, `model`, `benchmark_regression`, `code_test`, `unknown`, `success`.

## Offline regression benchmark

```bash
python sira.py benchmark --memory
```

The fixture checks repeated-failure deduplication, validated success retention, rejected-claim memory, and retrieval of a prior failure. It must report `api_requests: 0`.

## Limitations

v0.8 memory is structured experience, not proof that a causal explanation is correct. Failure classification is deterministic and conservative. Autonomous root-cause research, hypothesis generation and experiment execution belong to the next stage and must validate lessons before promotion.

## Improvement lifecycle (v0.9)

v0.9 adds an `experimenting` state and a deterministic improvement loop. The original run artifacts remain canonical; improvement plans/hypotheses/experiments are derived local artifacts under `improvements/`.

```text
observed → researched → experimenting → validated/rejected
experimenting → researched   # when the current baseline is unhealthy
rejected → researched
validated → superseded
```

Use:

```bash
python sira.py improve plan
python sira.py improve research <MEMORY_ID>
python sira.py improve experiment <HYPOTHESIS_ID>
python sira.py improve status
python sira.py benchmark --improvement
```

The v0.9 runner copies only public project code/tests/benchmarks/docs into an isolated candidate workspace. It does not copy `.env`, memory, runs, cache or Git metadata; it accepts no arbitrary shell command and performs no promotion. See `docs/superpowers/specs/2026-09-17-improvement-loop-design.md`.

## Autonomous target hygiene (v0.9.1)

Default `improve plan` selection excludes memories whose provider is clearly synthetic test data (`fixture`, `mock`, `dummy`, or `synthetic`). Those memories remain searchable and auditable; they are only excluded from autonomous target selection. A real `benchmark_regression` memory remains eligible because a failing regression suite is itself actionable evidence, even when the benchmark uses fixtures internally.


## Research context hygiene (v0.9.2)

`improve research <MEMORY_ID>` now filters related memory through the same autonomous hygiene boundary used by `improve plan`. Synthetic providers containing `fixture`, `mock`, `dummy`, or `synthetic` remain stored and searchable but do not influence autonomous root-cause context or `validated_successes_found`. Genuine `benchmark_regression` memories remain eligible.

The hypothesis fingerprint includes a research-context policy version. This intentionally invalidates stale pre-v0.9.2 hypotheses after the context policy changes, preventing an older hypothesis containing synthetic evidence from being silently reused.

## v1.3 Batch 1 — long-term memory quality foundation

Schema version 2 is an additive migration from schema version 1. Existing memories, occurrences and transition history are preserved.

New memory-quality metadata includes real versus synthetic occurrence counts, machine-memory origin classification, deterministic confidence in the range `0.0..1.0`, dynamic quality scoring with bounded freshness, contradiction/source-diversity fields for later enrichment, and explicit supersession lineage.

Synthetic fixture/mock/dummy memories remain searchable and auditable, but do not authorize autonomous improvement. A genuine `benchmark_regression` remains an exception because the regression itself is actionable evidence even when its suite uses fixtures internally.

Protected owner/user preferences are intentionally not stored through the machine-learning `Observation` namespace. This is data-plane separation only: any future writable owner-preference authority must be an owner-approved protected-shell change, not autonomously modifiable memory code.

The v1.3 memory benchmark adds deterministic invariants for validation strength, freshness, contradiction penalty, synthetic contamination protection, supersession lineage and protected-origin separation. It remains fully offline with zero API requests.
