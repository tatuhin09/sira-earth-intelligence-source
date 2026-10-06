# SIRA v1.0D-2 Unified Worker Integration Design

## Scope

Connect the v1.0D-1 unified target scheduler to the existing persistent `self on` worker without changing protected-shell boundaries. This stage reuses already-verified memory and opportunity pipelines instead of creating a new promotion path.

## Runtime contract

- Every persistent cycle begins with `select_autonomous_target()`.
- A selected memory target is pinned to the exact `memory_id` chosen by the unified scheduler and delegated to the existing memory improvement cycle.
- A selected opportunity runs: local evidence -> free/public research -> relevance gate -> writer handoff -> Evaluator 2 -> Evaluator 1 -> transactional promotion/rollback.
- No writer handoff begins after a `self off` request is observed at a stage checkpoint.
- Weak evidence or insufficient external research ends the cycle without writer/model use and records exact-opportunity cooldown.
- Unexpected rollback failure is treated as a worker-stopping error.
- Existing protected-shell and candidate workspace policy is unchanged.

## Audit contract

`runtime/last_cycle.json` records the target selection ID and, for opportunity work, opportunity/evidence/research/handoff IDs plus promotion outcome. `self status` exposes these IDs in the last-cycle summary.

## Deferred work

Resource-aware cadence/backoff, reboot recovery refinements, provider quota scheduling, and soak validation are deferred to v1.0D-3/v1.0D-4.
