# SIRA v1.8L — Final Operations and Release Acceptance

v1.8L is the final required operations-hardening stage for the current SIRA
architecture. It adds no new research, editing, promotion, payment, package
installation, or external authority.

## Final command surface

```bash
python sira.py self status
python sira.py self on
python sira.py self off
python sira.py self release-check
python sira.py notifications list
python sira.py notifications deliver
```

`self on` remains the explicit owner-controlled start command. `self off`
remains the explicit graceful stop command.

After a machine restart, running `self on` reuses the existing stale-worker,
interrupted-cycle, orphan-worker, and promotion-lease recovery paths. v1.8L
does not install an OS-level auto-start service or silently enable autonomy.

## Release gate

`self release-check` requires:

- runtime OFF/stopped before and after;
- healthy runtime state;
- a clean Git working tree;
- protected surface safe and unchanged;
- source surface unchanged by diagnostics;
- engineering canary 21/21;
- reliability diagnostic pass;
- multi-worker diagnostic pass;
- primary memory database healthy;
- latest memory backup healthy;
- primary/backup memory parity;
- at least two passed real bounded soak artifacts;
- at least one passed multi-cycle real soak;
- latest real soak passed;
- last cycle is not a failed/rollback-failed cycle;
- owner notification outbox readable.

Pending owner notifications do not fail release readiness. They are operational
work items, not evidence of runtime corruption.

## Memory

The release check is read-only for memory. It inspects:

- `memory/sira_memory.sqlite3`
- `memory/backups/sira_memory.latest.sqlite3`

using the existing memory-integrity implementation.

## Soak evidence

The release gate reuses the persisted v1.8K real-soak reports under
`runtime/soak/`. It does not fake or regenerate real-soak evidence.

## Reboot/restart semantics

SIRA remains owner-controlled rather than OS-autostarted. A reboot ends the
process. On the next explicit `self on`, the existing runtime recovers stale
state, interrupted cycles, orphaned worker tasks, and stale promotion leases
before new autonomous work proceeds.

## Release meaning

`decision_code = release_ready` means the current v1.x architecture has passed
its required local acceptance gates and may be used through `self on/off`.

Future Astra reviews, Tool Forge, additional providers, UI, and other advanced
features are optional enhancements, not blockers for this release.
