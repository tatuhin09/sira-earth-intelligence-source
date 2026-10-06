# Provider Readiness Diagnostic

`src/sira/provider_readiness.py` converts the static Provider Capability Catalog
into a local readiness snapshot.

It answers:

- which providers are usable without credentials;
- which optional credentials are configured or missing;
- which required credentials are missing;
- which providers are free vs metered;
- which providers are eligible for autonomous research.

## Security

The diagnostic checks credential **presence only**.

It never prints, serializes, persists, hashes, or transmits credential values.
Output may contain credential variable names such as `TAVILY_API_KEY`, but never
their contents.

The default credential source is the current process environment. This is
intentional: the diagnostic does not parse `.env` files or inspect unrelated
secret stores. A future Secrets/Capability Broker can call the same
`assess_provider(...)` API with its own presence-only mapping.

## Usage

All providers:

```bash
python src/sira/provider_readiness.py
```

One provider:

```bash
python src/sira/provider_readiness.py --provider github_public
```

## Exit status

- `0`: all providers in the requested snapshot are usable;
- `2`: unknown provider;
- `3`: one or more providers are blocked by a missing required credential.

An optional credential being absent does not block a provider.

## Important distinction

`cost_class = metered` is descriptive resource metadata. This diagnostic does
not authorize spending and does not override SIRA's separate spending/resource
policy. A provider can be credential-ready while still being disallowed by a
future policy gate.
