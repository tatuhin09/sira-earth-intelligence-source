# Memory Integrity / Migration Preflight

`src/sira/memory_integrity.py` is a read-only SQLite preflight utility for SIRA's
learning-memory database.

It is intentionally separate from schema migration code. Its job is to answer:

- does the primary database exist?
- does SQLite `PRAGMA quick_check` return `ok`?
- what memory schema version is recorded in `metadata`?
- how many `memories`, `occurrences`, and `transitions` rows exist?
- if a backup is supplied, does its schema and core row-count state match?
- is the pair suitable for a migration that requires exact pre-migration parity?

## Safety

The utility opens SQLite with `mode=ro`. It cannot create or mutate the database.
It does not print database row contents, memory text, API keys, or credentials.

## Usage

Primary health only:

```bash
python src/sira/memory_integrity.py --db /path/to/memory.sqlite
```

Primary + backup comparison:

```bash
python src/sira/memory_integrity.py \
  --db /path/to/current.sqlite \
  --backup /path/to/backup.sqlite \
  --require-parity
```

Exit codes:

- `0`: requested health/parity condition passed.
- `2`: primary database unhealthy.
- `3`: `--require-parity` requested but backup is missing or parity failed.
- `4`: backup supplied without `--require-parity`, but backup itself is unhealthy.

## Migration policy

For future schema migrations:

1. create/identify the pre-migration backup;
2. run this preflight with `--require-parity`;
3. migrate only after a successful report;
4. after migration, run a post-migration integrity check and compare preserved
   row counts/history using the migration's explicit expected schema change.

A schema mismatch is expected *after* a successful migration, so exact parity is
a pre-migration invariant, not a post-migration invariant.
