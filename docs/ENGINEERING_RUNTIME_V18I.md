# SIRA v1.8I — Bounded Autonomous Engineering Runtime Integration

v1.8I connects research-ready autonomous opportunities to the protected
multi-language engineering chain introduced in v1.8E–H.

## Routing

The existing legacy SIRA Python opportunity handoff remains available.

The engineering route is selected only when all of the following hold:

- the scheduler selected an opportunity;
- local evidence is research-ready;
- research is writer-ready and free/public-only;
- the target is a supported engineering source file;
- the target is not in `PROTECTED_PATHS`;
- the current project has a ready engineering edit policy.

Custom/injected legacy handoff runners used by existing tests and tools do not
silently enable the new route. Production defaults enable the engineering route.

## Authority chain

The runtime bridge assembles but does not weaken the existing chain:

`engineering writer -> isolated verification -> independent evaluator ->
protected checksum authorization -> protected transactional promotion`

No new write or promotion authority is created in the runtime layer.

The existing global promotion lease remains owned by `runtime.py`. The
engineering runtime bridge does not acquire a second lease.

## Stop semantics

The runtime guard is rechecked after writer verification, evaluator review, and
protected authorization but immediately before transactional promotion.

If `self off` has been requested by then, the engineering candidate is not
promoted and the cycle returns `stop_requested`.

## Metered model policy

The existing runtime code-generation broker and global metered budget remain in
front of both legacy and engineering code routes.

The metered attempt is recorded once before the selected handoff route executes.

## Workspace and audit

Engineering writer and promotion workspaces remain outside the main project,
under a bounded project-specific sibling state directory:

`<project-parent>/.sira-engineering-runtime/<project-hash>/runs/...`

The normal opportunity handoff audit remains inside
`improvements/opportunities/handoffs/`, preserving existing opportunity history
and feedback behavior.

## Unchanged components

v1.8I does not modify:

- `autonomous_promotion.py`
- `opportunity_handoff.py`
- `worker_coordination.py`
- v1.8E writer
- v1.8F evaluator
- v1.8G authorization
- v1.8H promotion/rollback
- package installation policy
- verification isolation policy

The new `engineering_runtime.py` module is itself added to `PROTECTED_PATHS`.
