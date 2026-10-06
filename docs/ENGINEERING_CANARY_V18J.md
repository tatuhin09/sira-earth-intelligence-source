# SIRA v1.8J — Controlled Autonomous Engineering Canary + Soak Readiness

v1.8J does not start persistent autonomy.

It adds a protected, offline `self canary-check` diagnostic that must pass
before a future bounded soak is allowed.

## Real-project guarantees

The canary requires the real SIRA runtime to already be OFF/stopped.

It records the real engineering source-surface checksum before and after the
diagnostic and fails readiness if that checksum changes.

The real project is never used as the canary promotion target.

## Temporary fixture canaries

Inside temporary projects the diagnostic exercises:

1. engineering runtime eligibility;
2. candidate generation;
3. isolated verification;
4. independent evaluator;
5. protected checksum authorization;
6. transactional promotion;
7. promotion audit persistence;
8. package-install and main-tree-execution prohibitions;
9. forced post-verification failure and automatic rollback;
10. rollback checksum verification;
11. stop request immediately before promotion;
12. one-cycle persistent-loop ownership and clean stop.

## Existing diagnostics reused

The canary also invokes the existing offline:

- reliability check;
- multi-worker coordination check.

Those checks validate resource pacing, interrupted-cycle recovery, isolated
readonly workers, serialized code work, promotion lease behavior and orphan
recovery.

## Zero-cost behavior

`self canary-check` performs:

- zero external API requests;
- zero model requests;
- zero metered attempt records;
- zero paid spending.

It uses only deterministic fixture model output and injected local command
runners.

## Readiness

A successful report returns:

`decision_code = bounded_soak_ready`

and:

`soak_readiness.ready_for_bounded_soak = true`

This is only a readiness signal. It does not call `self on`, does not schedule
background work and does not grant any additional promotion authority.

The new `src/sira/engineering_canary.py` module is protected.
