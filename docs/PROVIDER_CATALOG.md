# Provider Capability Catalog

`src/sira/provider_catalog.py` defines a small, normalized metadata contract for
SIRA's current external research providers.

It is deliberately **not** a router. It does not perform network requests,
change provider ordering in the autonomous runtime, or alter existing provider
implementations.

## Why this exists

Future systems such as the Capability Broker, Tool Registry, Strategy Learning,
resource policy, and control-center UI need one stable place to ask questions
such as:

- Which provider supports paper search?
- Which providers are public/free?
- Which provider requires a key?
- Which provider is metered?
- Which providers are read-only?
- Which credentials belong to a provider?
- Is the capability safe for autonomous research?

This catalog provides that descriptive layer without coupling those systems to
individual provider implementation classes.

## Current entries

- GitHub Public
- OpenAlex
- Crossref
- arXiv
- Semantic Scholar
- Tavily

## Safety invariants

Every entry in this catalog must remain read-only and must declare
`executes_remote_code = false`.

Public GitHub repository content is still untrusted input. Catalog membership
does not authorize execution, package installation, repository mutation, or
copying code into SIRA's main tree.

## Resource policy

Providers are described as either:

- `free`
- `metered`

The helper `free_first(...)` gives future orchestration a deterministic baseline
consistent with SIRA's free/local-first policy. This helper is descriptive
groundwork only; the existing runtime is not rewired in this step.

## Diagnostic snapshot

Run:

```bash
python src/sira/provider_catalog.py
```

The output is JSON and contains no credential values.
