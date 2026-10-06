# SIRA v1.8K — Bounded Real Autonomous Soak

v1.8K adds a protected foreground soak controller for the real SIRA project.

It does not start the long-running background worker. Instead it arms the normal
runtime state, calls the production `run_autonomous_loop`, and executes the
production `run_unified_improvement_cycle` for a strict bounded number of
cycles.

## Command

```bash
python sira.py self soak-run --cycles 2 --max-seconds 900
```

Allowed cycle count: 1–3.

Allowed wall-clock bound: 30–1800 seconds.

## What remains real

The soak uses the normal production path, including:

- autonomous target selection;
- memory/opportunity routing;
- free/public research;
- owner-access deferral;
- metered-budget checks and pacing;
- multi-worker coordination;
- single promotion lease;
- protected engineering writer/evaluator/authorization;
- transactional promotion and rollback;
- owner notifications;
- interrupted-cycle recovery.

No gate is bypassed.

## Automatic stop

After the requested cycle count, the controller writes `desired_state=off`.
The existing autonomous loop observes that state and exits through its normal
cleanup path.

A wall-clock deadline also requests a clean stop. A deadline that expires before
any cycle completes is inconclusive rather than a successful soak.

## Integrity checks

The report binds:

- runtime OFF before and after;
- protected physical files before and after;
- engineering source-surface checksums before and after;
- cycle outcomes;
- promotion/main-tree mutation flags;
- resource usage;
- recovery information.

A source-surface change is accepted only when a cycle reports both
`promotion_performed=true` and `main_tree_modified=true`.

Any failed cycle, rollback failure, protected-shell change, unauthorized source
mutation, or unclean runtime stop fails the soak.

## Authority

The soak controller creates no promotion, payment, package-installation, or
network authority. Existing broker/access/budget/protected gates remain the
only authority boundaries.

The new `src/sira/runtime_soak.py` module is protected.
