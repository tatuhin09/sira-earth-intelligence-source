# Provider Diagnostics Summary

`src/sira/provider_diagnostics.py` consolidates the provider foundation into one
read-only JSON contract suitable for diagnostics and a future local Control
Center.

It composes the existing modules instead of creating parallel state:

- Provider Catalog
- Provider Readiness
- Provider Health
- Provider Router Advisory

## Purpose

The output answers, in one place:

- which providers exist;
- which credentials are required or optional;
- which providers are currently available;
- which are blocked or cooling down;
- whether recurring historical unreliability exists;
- which providers are advised for each capability.

## Safety / resource boundary

The diagnostics contract:

- makes no provider network requests;
- performs no paid spending;
- never serializes credential values;
- does not mutate routing;
- does not write provider state;
- does not record metered attempts;
- does not replace current runtime routers.

It is a presentation/inspection contract only.

## Usage

```bash
python src/sira/provider_diagnostics.py --root ~/sira
```

Metered-eligible diagnostic view:

```bash
python src/sira/provider_diagnostics.py   --root ~/sira   --allow-metered
```

`--allow-metered` only changes policy eligibility in the diagnostic view.
Required credentials and the existing runtime metered budget still apply.

## Future Control Center

The schema `sira.provider_diagnostics.v1` can later back a Control Center panel
without the UI needing to know internal provider files or runtime state layouts.

Heavy routing integration, Strategy Learning, and Capability Broker behavior
remain separate future milestones.
