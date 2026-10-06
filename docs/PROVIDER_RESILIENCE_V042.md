# SIRA v0.4.2 — Provider-aware autonomous worker resilience

This repair addresses the first persistent autonomous run failure observed on
generation 9.

## Root cause

The engineering code worker raised `ProviderError`, but the multi-worker layer
persisted only the Python exception class name. The runtime then converted the
failure to a generic `MultiWorkerError`. Runtime reliability therefore lost
the provider error code and classified the cycle as an internal fault, which
stopped the persistent worker.

## Repair

- Preserve only bounded/sanitized `ProviderError` metadata:
  `code`, `request_sent`, `retry_after`, and `request_count`.
- Do not persist raw provider bodies, exception messages, credentials, or
  prompts in worker failure metadata.
- Surface the structured provider failure in the coordination report.
- Propagate that provider failure into the autonomous cycle so the existing
  reliability classifier can apply provider/transient behavior.
- Keep non-provider exceptions fail-closed as internal failures.
- Count failed provider request attempts in cycle resource usage.
- Preserve existing metered-attempt budget controls.
- Fix the Desktop refresh path that still referenced the removed legacy
  `research-detail` element.

No new model, payment, promotion, package-install, or runtime-control authority
is introduced.
