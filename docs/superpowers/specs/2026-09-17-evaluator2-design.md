# SIRA v1.0B-2 Evaluator 2 Design

## Scope
v1.0B-2 adds a deterministic second-stage candidate evaluator. It inspects the isolated candidate from the trusted main process, compares it with the current public tree, combines that structural diff with unit-test/benchmark health, and emits an auditable accept/reject/block report. It does not promote code.

## Decision model
Evaluator 2 blocks when the current baseline is unhealthy, rejects when the candidate regresses or violates the structural boundary, accepts an unchanged candidate only as `no_change_verified`, and accepts a changed candidate only when the diff is bounded, text-only, confined to approved modifiable roots, leaves protected paths unchanged, and candidate tests/benchmarks pass.

## Structural inspection
The evaluator records added, modified, removed and symlink paths. Protected-path changes, non-modifiable-path changes, removals, symlinks, non-UTF-8/NUL content, excessive changed-file counts and excessive changed bytes are explicit risk flags. Runtime artifacts such as `__pycache__` are ignored.

## Trust boundary
Evaluation code is loaded from the trusted running/main SIRA process, not from the candidate. `evaluator2.py` may remain evolvable in future candidates, but a candidate cannot change the evaluator that judges that same experiment. Evaluator 1 and promotion/rollback remain deferred to v1.0B-3.

## Persistence
Every improvement experiment persists the complete Evaluator-2 report. `promotion_performed` stays false in this stage.
