# SIRA v1.0B-3a Evaluator 1 Final Gate Design

## Scope

v1.0B-3a adds a protected, deterministic final gate that runs from the trusted current/main SIRA process after Evaluator 2. It does not promote or modify the main tree. Its job is to independently re-check the invariants that must remain true immediately before a future promotion step and to emit a content-bound authorization record.

## Trust boundary

`src/sira/evaluator1.py` is part of the protected shell already listed in `PROTECTED_PATHS`. A candidate may evolve ordinary SIRA code and Evaluator 2 for future generations, but it cannot edit Evaluator 1, runtime owner control, self-modification boundary, future promotion/rollback logic, identity, audit integrity, or safety boundary through the autonomous candidate editor.

Evaluator 1 accepts only the currently supported Evaluator-2 policy version, requires an actual `candidate_verified` recommendation, independently verifies the candidate manifest against the current main public-tree digest, independently compares the protected shell between main and candidate, rejects public-tree symlinks, and rejects verification shrinkage such as a lower candidate test count or fewer passing benchmark cases.

## Output

The gate report contains the current-main public digest, final candidate public digest, protected-shell digest, Evaluator-2 report digest, and a deterministic authorization fingerprint. The fingerprint is an audit checksum, not a cryptographic signature or bearer token. Future promotion must recompute these facts rather than trusting a stale report blindly.

`promotion_allowed` may become true, but `promotion_performed` remains false in v1.0B-3a.
