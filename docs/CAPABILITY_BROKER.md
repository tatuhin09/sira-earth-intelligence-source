# Capability Broker v1.6

Capability Broker is a bounded selection/preflight layer. It does not execute
providers, create access requests, spend money, or grant promotion authority.

## Autonomous research

Opportunity research uses broker-selected free/public paper providers. Consumer
implementations remain explicit; unsupported catalog providers are audit-visible
but are not instantiated by a consumer that has not integrated them.

## Autonomous model preflight

Gemini is cataloged as a metered, key-required capability for `code_generation`
and `evidence_synthesis`.

The autonomous code phase performs a broker preflight before a metered attempt
is recorded. The preflight receives credential presence metadata only, never the
secret value.

Missing Gemini access becomes metadata-only AccessNeed. The existing runtime
access boundary creates the owner request. If the runtime metered budget is
closed, model execution is deferred without creating an owner-access request.

## Explicit dependency consumers

These contracts remain dependency-injected:

- `papers_run(..., provider, ...)`
- `read_run(..., reader, ...)`
- `answer_run(..., model, ...)`

The broker does not silently replace caller-selected dependencies.
