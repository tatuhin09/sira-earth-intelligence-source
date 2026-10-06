# SIRA v1.8G — Protected Engineering Authorization Gate

v1.8G adds the first protected-shell component for the parallel engineering
path.

`src/sira/engineering_authorization.py` is added to `PROTECTED_PATHS`, so both
the existing SIRA self-modification editor and the v1.8D engineering editor
treat it as immutable.

The protected gate independently re-checks the v1.8E writer attempt and v1.8F
evaluator recommendation. It does not simply trust an `accept` field.

The gate independently binds:

- the writer-attempt and evaluator report;
- the exact current main source-copy surface;
- the exact candidate source-copy surface;
- the original candidate manifest source SHA;
- the exact declared changed file list;
- the SHA-256 of every changed candidate file;
- the actual main-to-candidate diff;
- no file removals;
- no symlinks, sensitive artifacts, or unexpected surface files;
- UTF-8 text and v1.8 edit byte/count bounds;
- clean executed isolated verification;
- zero structured diagnostics;
- the v1.8F evaluator's source/diff binding.

A successful decision returns:

- `decision = allow`
- `decision_code = engineering_promotion_authorized`
- `promotion_allowed = true`

but this is checksum authorization only. The authorization object explicitly
keeps:

- `write_authority = false`
- `promotion_execution_authority = false`
- `checksum_only = true`

The checksum fingerprint binds the evaluator report, writer attempt, current
main source surface, candidate source surface, changed-file hash set, and
verification result.

This stage does not write to the engineering main tree and does not implement
engineering promotion or rollback. Existing `evaluator1.py`, `evaluator2.py`,
`promotion.py`, `rollback.py`, and `autonomous_promotion.py` remain unchanged.

The next stage can implement a separate protected transactional engineering
promotion/rollback primitive that must consume a fresh v1.8G authorization.
