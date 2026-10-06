# SIRA v0.8 Failure Learning + Persistent Experiment Memory Design

## Goal
Persist SIRA run outcomes as structured, searchable local learning memory so later autonomous research can reuse prior failures and successful strategies without repeating work.

## Architecture
- SQLite lives at `memory/sira_memory.sqlite3`; JSON run artifacts remain canonical provenance.
- `learn <RUN_ID>` extracts deterministic observations from one existing run artifact and upserts deduplicated memories by a stable fingerprint.
- `learn --all` ingests all compatible local runs without network access.
- Memories retain every occurrence and status transition; records are never silently deleted.
- Successful outcomes begin as `validated`; failures/rejections begin as `observed`.
- SQLite runs WAL mode and maintains a transactionally generated latest backup; corruption is quarantined and the latest valid backup restored automatically.

## Memory model
A memory has: id, fingerprint, kind, category, capability, provider, error code, summary, status, timestamps, and occurrence count. Occurrences preserve run id, artifact name/hash, outcome status, and bounded JSON details. Transitions preserve append-only state history.

Statuses: `observed`, `researched`, `validated`, `rejected`, `superseded`.
Categories: `network`, `rate_limit`, `bad_source`, `provider`, `parsing`, `verification`, `citation`, `model`, `benchmark_regression`, `code_test`, `unknown`, `success`.

## CLI
- `python sira.py learn <RUN_ID>`
- `python sira.py learn --all`
- `python sira.py memory search "rate limit"`
- `python sira.py memory show <MEMORY_ID>`
- `python sira.py memory stats`
- `python sira.py benchmark --memory`

## Safety / bounds
No network calls, no model calls, no subprocess execution. Run ids and memory ids are validated. Artifacts must be regular non-symlink files under `runs/`, capped in size, valid JSON, and self-identify with the requested run id where applicable. Memory details are bounded. Corruption recovery never overwrites the only copy: the broken DB is quarantined before backup restore.
