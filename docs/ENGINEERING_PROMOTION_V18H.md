# SIRA v1.8H — Protected Transactional Engineering Promotion + Rollback

v1.8H adds the protected execution primitive for the parallel engineering path.

It does **not** modify or reuse the existing SIRA Python `promotion.py`
authorization model. Instead, `src/sira/engineering_promotion.py` is a new
protected-shell module and consumes the v1.8G engineering authorization.

## Fresh authorization consumption

Immediately before any write, the promotion module recomputes v1.8G
authorization from the current main project, current candidate, writer attempt,
and engineering evaluator report.

The supplied authorization must match the fresh authorization on:

- authorization fingerprint;
- writer-attempt digest;
- evaluator-report digest;
- current main source-surface checksum;
- candidate source-surface checksum;
- changed-file-set checksum;
- verification checksum;
- exact changed file list and per-file SHA-256 binding.

A stale main project, changed candidate, modified evaluator evidence, or
tampered authorization is denied before source-file writes.

## Transaction

For an authorized candidate:

1. preflight every changed source and destination path;
2. verify every candidate file is regular UTF-8 text and matches its authorized
   SHA-256;
3. create an exact rollback backup for the changed file set;
4. atomically replace only those authorized source files;
5. require the resulting main engineering source surface to exactly equal the
   authorized candidate checksum;
6. create a fresh isolated copy of the promoted main source surface outside the
   main project;
7. run the existing v1.8B isolated verifier against that fresh copy;
8. require zero diagnostics and a fully passed verification result;
9. re-check main, original candidate, and post-verification copy checksums;
10. commit the transaction report only after those checks.

No build/test command executes inside the main project tree.

## Rollback

Any source write failure, checksum mismatch, fresh-copy failure, verifier
exception, or failed post-promotion verification restores the exact pre-write
files using the existing protected rollback primitives.

Files that did not exist before promotion are removed during rollback.

Rollback is checksum-verified against the complete pre-promotion engineering
source surface. If rollback cannot be verified, the report explicitly marks
manual recovery required and retains the backup.

## Boundaries

The promotion path keeps:

- package installation disabled;
- shell execution controlled by the existing engineering verifier;
- dependency-manifest editing disabled by the earlier engineering editor and
  authorization gates;
- autonomous engineering runtime integration disabled in this stage.

`engineering_promotion.py` is added to `PROTECTED_PATHS`. Existing
`promotion.py`, `rollback.py`, `evaluator1.py`, `evaluator2.py`,
`autonomous_promotion.py`, v1.8G authorization, and the engineering writer /
verifier / evaluator remain unchanged.

The next stage can integrate this protected transaction into a bounded
engineering runtime workflow without weakening the protected gate.
